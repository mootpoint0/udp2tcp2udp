"""
udp2tcp2udp server.

Runs on a machine that can reach the game server. It accepts TCP connections
("tunnels") from client.py, unwraps each framed packet and sends it to the game
server over UDP. Replies from the game server are wrapped again and sent back
through the tunnel.

    Game client <==UDP==> client.py <==TCP==> server.py <==UDP==> Game server

Each game client gets its own "session": a dedicated UDP socket towards the
game server, so the game server sees every player as a separate address, just
as if they had connected to it directly. Sessions are not tied to a single
tunnel, so a player keeps their session if client.py reconnects quickly.
"""

import socket
import struct
import time
from threading import Thread, Lock

# Address and port to accept tunnel connections from client.py on
LOCAL_IP = "0.0.0.0"
LOCAL_TCP_PORT = 4668

# Address and port of the game server (or any other UDP service) to forward traffic to
REMOTE_UDP_IP = '10.0.0.8'
REMOTE_UDP_PORT = 2667

# Seconds between heartbeats, and how long a tunnel may be silent before it is
# considered dead and closed. Must match client.py.
KEEPALIVE_INTERVAL = 5
KEEPALIVE_TIMEOUT = 20

# Seconds of no traffic in either direction before a game client's UDP socket is closed.
# Sessions outlive their tunnel, so a client that reconnects quickly keeps the same
# source port towards the game server and is not kicked.
SESSION_TIMEOUT = 60

# Every packet sent through the tunnel is framed as:
#   client IPv4 address (4 bytes), client UDP port (2 bytes), payload length (2 bytes), payload
# All header fields are in network byte order. A frame with port 0 and no
# payload is a heartbeat and carries no data.
HEADER = struct.Struct('!4sHH')
HEARTBEAT = HEADER.pack(bytes(4), 0, 0)

# Largest possible UDP datagram, so no packet is ever truncated
MAX_UDP_SIZE = 65535

# Listening socket for incoming tunnel connections
tcp_sock = socket.socket()

# Active sessions, keyed by the game client's (ip, port) as seen by client.py.
# sessions_lock guards the dict and each session's tunnel and last_active fields.
sessions = {}
sessions_lock = Lock()


class Tunnel:
    """One TCP connection from client.py.

    Several threads (one per session, plus the heartbeat) send replies through
    the same connection, so writes are serialized with a lock to keep frames
    from interleaving.
    """

    def __init__(self, connection, address):
        self.connection = connection
        self.address = address
        self.lock = Lock()
        # Set to False once the connection is closed; sends after that are dropped
        self.alive = True

    def send(self, frame):
        """Send a framed message, closing the tunnel if the send fails."""
        with self.lock:
            if not self.alive:
                return
            try:
                self.connection.sendall(frame)
            except OSError:
                self.close_locked()

    def close(self):
        """Close the tunnel. Safe to call more than once."""
        with self.lock:
            self.close_locked()

    def close_locked(self):
        """Close the tunnel. The caller must already hold self.lock."""
        if not self.alive:
            return
        self.alive = False
        # shutdown() wakes up any thread blocked in recv() on this connection
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()


class Session:
    """The state for one game client.

    Holds the UDP socket used to talk to the game server on that client's
    behalf, and the tunnel its replies should be sent back through.
    """

    def __init__(self, client_ip, client_port, tunnel):
        self.client_ip = client_ip
        self.client_port = client_port
        # The tunnel the client most recently sent through; replies go here
        self.tunnel = tunnel
        # When traffic last passed in either direction, for idle timeout
        self.last_active = time.monotonic()
        # Port 0 lets the OS pick a free port, giving each client its own source port
        self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_socket.bind((LOCAL_IP, 0))
        # Wake up regularly so receive_out() can check whether the session went idle
        self.udp_socket.settimeout(5)


def recv_exact(connection, size):
    """Read exactly `size` bytes from a TCP socket.

    TCP is a byte stream, so a single recv() can return part of a frame or
    several frames at once. This keeps reading until the requested amount has
    arrived.

    Raises ConnectionError if the other side closes the connection, and
    socket.timeout if nothing arrives within KEEPALIVE_TIMEOUT.
    """
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise ConnectionError('tunnel closed by client')
        data += chunk
    return bytes(data)


def forward(tunnel, client_ip, client_port, data):
    """Send a game client's packet to the game server from that client's session.

    Creates the session (and its reply thread) the first time a client is seen.
    """
    key = (client_ip, client_port)

    with sessions_lock:
        session = sessions.get(key)
        if session is None:
            session = Session(client_ip, client_port, tunnel)
            sessions[key] = session
            print(f'New session {client_ip}:{client_port} via local port {session.udp_socket.getsockname()[1]}')
            Thread(target=receive_out, args=(session,), daemon=True).start()
        # Replies follow whichever tunnel the client most recently used
        session.tunnel = tunnel
        session.last_active = time.monotonic()

    try:
        session.udp_socket.sendto(data, (REMOTE_UDP_IP, REMOTE_UDP_PORT))
    except OSError:
        # The socket may have just been closed by an idle timeout; UDP delivery
        # isn't guaranteed anyway, so drop the packet
        pass


def receive_out(session):
    """Relay replies from the game server back through the session's tunnel.

    Runs in its own thread for each session. Removes the session and returns
    once it has been idle for longer than SESSION_TIMEOUT.
    """
    while True:
        try:
            data, address = session.udp_socket.recvfrom(MAX_UDP_SIZE)
        except socket.timeout:
            # Checked under the lock so forward() can't pick up the session
            # while it is being removed
            with sessions_lock:
                if time.monotonic() - session.last_active > SESSION_TIMEOUT:
                    del sessions[(session.client_ip, session.client_port)]
                    session.udp_socket.close()
                    print(f'Session {session.client_ip}:{session.client_port} timed out')
                    return
            continue
        except ConnectionResetError:
            # Windows reports an ICMP "port unreachable" (e.g. game server restarting)
            # as an error on the next recvfrom(); it is harmless, so ignore it
            continue
        except OSError:
            return

        session.last_active = time.monotonic()
        respond(session.tunnel, session.client_ip, session.client_port, data)


def respond(tunnel, client_ip, client_port, data):
    """Wrap a reply in a header addressed to the game client and send it through the tunnel."""
    tunnel.send(HEADER.pack(socket.inet_aton(client_ip), client_port, len(data)) + data)


def heartbeat(tunnel):
    """Send a heartbeat through the tunnel every KEEPALIVE_INTERVAL seconds until it closes.

    This lets client.py tell a quiet but healthy tunnel apart from a dead one,
    and stops SSH tunnels and firewalls from closing the connection for being idle.
    """
    while tunnel.alive:
        time.sleep(KEEPALIVE_INTERVAL)
        tunnel.send(HEARTBEAT)


def receive_in(connection, address):
    """Handle one tunnel connection from client.py until it closes.

    Reads frames from the tunnel and forwards each one to the game server.
    Sessions are left open when the tunnel closes so that a reconnecting
    client can pick them back up; they are cleaned up by their idle timeout.
    """
    # Send each packet immediately instead of batching small packets (Nagle's
    # algorithm), which would add latency to game traffic
    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    # Let the OS detect connections that died without being closed
    connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    # recv() fails if not even a heartbeat arrives in this time, and sendall()
    # fails if the connection stays blocked this long
    connection.settimeout(KEEPALIVE_TIMEOUT)

    tunnel = Tunnel(connection, address)
    Thread(target=heartbeat, args=(tunnel,), daemon=True).start()
    print(f'Tunnel opened from {address[0]}:{address[1]}')

    try:
        while True:
            ip, port, length = HEADER.unpack(recv_exact(connection, HEADER.size))
            data = recv_exact(connection, length) if length else b''

            # Heartbeats only keep the connection alive; there is nothing to forward
            if port == 0:
                continue

            forward(tunnel, socket.inet_ntoa(ip), port, data)
    except OSError as e:
        print(f'Tunnel from {address[0]}:{address[1]} closed ({e})')
    finally:
        tunnel.close()


def main():
    """Accept tunnel connections until Ctrl+C, handling each one in its own thread."""
    # Allow restarting the server right away without waiting for old connections to time out
    tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp_sock.bind((LOCAL_IP, LOCAL_TCP_PORT))
    tcp_sock.listen(5)
    # A timeout lets Ctrl+C interrupt accept() on Windows
    tcp_sock.settimeout(1)
    print(f'Listening for tunnels on {LOCAL_IP}:{LOCAL_TCP_PORT}')

    try:
        while True:
            try:
                connection, address = tcp_sock.accept()
            except socket.timeout:
                continue
            # Daemon threads end automatically when the main thread exits
            Thread(target=receive_in, args=(connection, address), daemon=True).start()
    except KeyboardInterrupt:
        pass


main()
