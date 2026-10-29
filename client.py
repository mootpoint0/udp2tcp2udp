"""
udp2tcp2udp client.

Runs on the machine next to the game client. It listens for UDP packets from
game clients, wraps each one in a small header and sends it through a single
TCP connection (the "tunnel") to server.py. Replies coming back through the
tunnel are unwrapped and sent to the right game client over UDP.

    Game client <==UDP==> client.py <==TCP==> server.py <==UDP==> Game server

If the tunnel drops, packets are discarded (as they would be on a lossy UDP
link) while the client reconnects in the background.
"""

import socket
import struct
import time
from threading import Thread, Lock

# Address and port that game clients send their UDP traffic to
LOCAL_IP = '0.0.0.0'
LOCAL_UDP_PORT = 4667

# Address and port of the machine running server.py (or of an SSH tunnel that leads to it)
SERVER_IP = '127.0.0.1'
SERVER_PORT = 4668

# Seconds between heartbeats, and how long the tunnel may be silent before it is
# considered dead and reconnected. Must match server.py.
KEEPALIVE_INTERVAL = 5
KEEPALIVE_TIMEOUT = 20

# Reconnect backoff in seconds (doubles on every failed attempt)
RECONNECT_DELAY_MIN = 1
RECONNECT_DELAY_MAX = 30

# Every packet sent through the tunnel is framed as:
#   client IPv4 address (4 bytes), client UDP port (2 bytes), payload length (2 bytes), payload
# All header fields are in network byte order. A frame with port 0 and no
# payload is a heartbeat and carries no data.
HEADER = struct.Struct('!4sHH')
HEARTBEAT = HEADER.pack(bytes(4), 0, 0)

# Largest possible UDP datagram, so no packet is ever truncated
MAX_UDP_SIZE = 65535

# The one UDP socket that all game clients talk to. Replies are sent back from
# it too, so game clients see them coming from the address they sent to.
udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# The current TCP connection to server.py, or None while reconnecting.
# tunnel_lock guards it and keeps frames from different threads from interleaving.
tunnel = None
tunnel_lock = Lock()


def recv_exact(r_socket, size):
    """Read exactly `size` bytes from a TCP socket.

    TCP is a byte stream, so a single recv() can return part of a frame or
    several frames at once. This keeps reading until the requested amount has
    arrived.

    Raises ConnectionError if the other side closes the connection, and
    socket.timeout if nothing arrives within KEEPALIVE_TIMEOUT.
    """
    data = bytearray()
    while len(data) < size:
        chunk = r_socket.recv(size - len(data))
        if not chunk:
            raise ConnectionError('tunnel closed by server')
        data += chunk
    return bytes(data)


def send_raw(frame):
    """Send an already framed message through the tunnel.

    The frame is dropped if the tunnel is currently down. If sending fails, the
    tunnel is shut down so the reader thread in tunnel_loop() notices and
    reconnects.
    """
    global tunnel
    with tunnel_lock:
        if tunnel is None:
            # Tunnel is down; drop the packet like a lossy UDP link would
            return
        try:
            tunnel.sendall(frame)
        except OSError:
            # Unblock the reader thread so the tunnel gets reconnected
            try:
                tunnel.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            tunnel = None


def forward(client_ip, client_port, data):
    """Wrap a UDP packet from a game client in a header and send it to server.py.

    The client's address goes in the header so server.py can keep a separate
    session per game client, and so replies can be routed back to it.
    """
    send_raw(HEADER.pack(socket.inet_aton(client_ip), client_port, len(data)) + data)


def heartbeat():
    """Send a heartbeat through the tunnel every KEEPALIVE_INTERVAL seconds.

    This lets server.py tell a quiet but healthy tunnel apart from a dead one,
    and stops SSH tunnels and firewalls from closing the connection for being idle.
    """
    while True:
        time.sleep(KEEPALIVE_INTERVAL)
        send_raw(HEARTBEAT)


def receive_out(r_socket):
    """Read frames from server.py and pass each reply on to its game client.

    Runs until the tunnel fails, then raises (OSError, ConnectionError or
    socket.timeout) so tunnel_loop() can reconnect.
    """
    while True:
        ip, port, length = HEADER.unpack(recv_exact(r_socket, HEADER.size))
        data = recv_exact(r_socket, length) if length else b''

        # Heartbeats only keep the connection alive; there is nothing to deliver
        if port == 0:
            continue

        respond(port, socket.inet_ntoa(ip), data)


def respond(client_port, client_ip, data):
    """Send a reply from the game server back to a game client over UDP."""
    try:
        udp_socket.sendto(data, (client_ip, client_port))
    except OSError:
        # The game client may have gone away; UDP delivery isn't guaranteed anyway
        pass


def connect_tunnel():
    """Open and configure a new TCP connection to server.py."""
    tcp_socket = socket.create_connection((SERVER_IP, SERVER_PORT), timeout=10)
    # Send each packet immediately instead of batching small packets (Nagle's
    # algorithm), which would add latency to game traffic
    tcp_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    # Let the OS detect connections that died without being closed
    tcp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    # recv() fails if not even a heartbeat arrives in this time, and sendall()
    # fails if the connection stays blocked this long
    tcp_socket.settimeout(KEEPALIVE_TIMEOUT)
    return tcp_socket


def tunnel_loop():
    """Keep a tunnel to server.py open, reconnecting whenever it drops.

    Runs forever in its own thread. While connected, it reads replies with
    receive_out(). When the connection fails, it waits and tries again, doubling
    the delay after each failed attempt up to RECONNECT_DELAY_MAX.
    """
    global tunnel
    delay = RECONNECT_DELAY_MIN
    while True:
        try:
            tcp_socket = connect_tunnel()
        except OSError as e:
            print(f'Could not connect to {SERVER_IP}:{SERVER_PORT} ({e}), retrying in {delay}s')
            time.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX)
            continue

        print(f'Tunnel connected to {SERVER_IP}:{SERVER_PORT}')
        delay = RECONNECT_DELAY_MIN
        # Make the new connection available to forward() and heartbeat()
        with tunnel_lock:
            tunnel = tcp_socket

        try:
            receive_out(tcp_socket)
        except OSError as e:
            print(f'Tunnel lost ({e}), reconnecting')

        # send_raw() may already have cleared it; only clear it if it's still ours
        with tunnel_lock:
            if tunnel is tcp_socket:
                tunnel = None
        tcp_socket.close()
        time.sleep(delay)


def receive_in(r_socket):
    """Wait for one UDP packet from a game client and forward it through the tunnel.

    Returns without doing anything if no packet arrives within the socket timeout.
    """
    try:
        data, address = r_socket.recvfrom(MAX_UDP_SIZE)
    except socket.timeout:
        return
    except ConnectionResetError:
        # Windows reports an ICMP "port unreachable" from a closed game client
        # as an error on the next recvfrom(); it is harmless, so ignore it
        return

    forward(address[0], address[1], data)


def main():
    """Start the tunnel and heartbeat threads, then forward game client packets until Ctrl+C."""
    udp_socket.bind((LOCAL_IP, LOCAL_UDP_PORT))
    # A timeout lets Ctrl+C interrupt the main loop on Windows
    udp_socket.settimeout(1)

    # Daemon threads end automatically when the main thread exits
    Thread(target=tunnel_loop, daemon=True).start()
    Thread(target=heartbeat, daemon=True).start()

    print(f'Listening for UDP on {LOCAL_IP}:{LOCAL_UDP_PORT}')
    try:
        while True:
            receive_in(udp_socket)
    except KeyboardInterrupt:
        pass


main()
