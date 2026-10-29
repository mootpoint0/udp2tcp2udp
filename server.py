import socket
import struct
import time
from threading import Thread, Lock

LOCAL_IP = "0.0.0.0"
LOCAL_TCP_PORT = 4668

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
# A frame with port 0 and no payload is a heartbeat.
HEADER = struct.Struct('!4sHH')
HEARTBEAT = HEADER.pack(bytes(4), 0, 0)
MAX_UDP_SIZE = 65535

tcp_sock = socket.socket()

# (client_ip, client_port) -> Session
sessions = {}
sessions_lock = Lock()


class Tunnel:
    def __init__(self, connection, address):
        self.connection = connection
        self.address = address
        self.lock = Lock()
        self.alive = True

    def send(self, frame):
        with self.lock:
            if not self.alive:
                return
            try:
                self.connection.sendall(frame)
            except OSError:
                self.close_locked()

    def close(self):
        with self.lock:
            self.close_locked()

    def close_locked(self):
        if not self.alive:
            return
        self.alive = False
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()


class Session:
    def __init__(self, client_ip, client_port, tunnel):
        self.client_ip = client_ip
        self.client_port = client_port
        self.tunnel = tunnel
        self.last_active = time.monotonic()
        self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_socket.bind((LOCAL_IP, 0))
        self.udp_socket.settimeout(5)


def recv_exact(connection, size):
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise ConnectionError('tunnel closed by client')
        data += chunk
    return bytes(data)


def forward(tunnel, client_ip, client_port, data):
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
        pass


def receive_out(session):
    while True:
        try:
            data, address = session.udp_socket.recvfrom(MAX_UDP_SIZE)
        except socket.timeout:
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
    tunnel.send(HEADER.pack(socket.inet_aton(client_ip), client_port, len(data)) + data)


def heartbeat(tunnel):
    while tunnel.alive:
        time.sleep(KEEPALIVE_INTERVAL)
        tunnel.send(HEARTBEAT)


def receive_in(connection, address):
    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    connection.settimeout(KEEPALIVE_TIMEOUT)

    tunnel = Tunnel(connection, address)
    Thread(target=heartbeat, args=(tunnel,), daemon=True).start()
    print(f'Tunnel opened from {address[0]}:{address[1]}')

    try:
        while True:
            ip, port, length = HEADER.unpack(recv_exact(connection, HEADER.size))
            data = recv_exact(connection, length) if length else b''

            if port == 0:
                continue

            forward(tunnel, socket.inet_ntoa(ip), port, data)
    except OSError as e:
        print(f'Tunnel from {address[0]}:{address[1]} closed ({e})')
    finally:
        tunnel.close()


def main():
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
            Thread(target=receive_in, args=(connection, address), daemon=True).start()
    except KeyboardInterrupt:
        pass


main()
