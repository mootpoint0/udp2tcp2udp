import socket
import struct
import time
from threading import Thread, Lock

LOCAL_IP = '0.0.0.0'
LOCAL_UDP_PORT = 4667

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
# A frame with port 0 and no payload is a heartbeat.
HEADER = struct.Struct('!4sHH')
HEARTBEAT = HEADER.pack(bytes(4), 0, 0)
MAX_UDP_SIZE = 65535

udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# The current TCP connection to server.py, or None while reconnecting.
# tunnel_lock guards it and keeps frames from different threads from interleaving.
tunnel = None
tunnel_lock = Lock()


def recv_exact(r_socket, size):
    data = bytearray()
    while len(data) < size:
        chunk = r_socket.recv(size - len(data))
        if not chunk:
            raise ConnectionError('tunnel closed by server')
        data += chunk
    return bytes(data)


def send_raw(frame):
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
    send_raw(HEADER.pack(socket.inet_aton(client_ip), client_port, len(data)) + data)


def heartbeat():
    while True:
        time.sleep(KEEPALIVE_INTERVAL)
        send_raw(HEARTBEAT)


def receive_out(r_socket):
    while True:
        ip, port, length = HEADER.unpack(recv_exact(r_socket, HEADER.size))
        data = recv_exact(r_socket, length) if length else b''

        if port == 0:
            continue

        respond(port, socket.inet_ntoa(ip), data)


def respond(client_port, client_ip, data):
    try:
        udp_socket.sendto(data, (client_ip, client_port))
    except OSError:
        pass


def connect_tunnel():
    tcp_socket = socket.create_connection((SERVER_IP, SERVER_PORT), timeout=10)
    tcp_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    tcp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    tcp_socket.settimeout(KEEPALIVE_TIMEOUT)
    return tcp_socket


def tunnel_loop():
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
        with tunnel_lock:
            tunnel = tcp_socket

        try:
            receive_out(tcp_socket)
        except OSError as e:
            print(f'Tunnel lost ({e}), reconnecting')

        with tunnel_lock:
            if tunnel is tcp_socket:
                tunnel = None
        tcp_socket.close()
        time.sleep(delay)


def receive_in(r_socket):
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
    udp_socket.bind((LOCAL_IP, LOCAL_UDP_PORT))
    # A timeout lets Ctrl+C interrupt the main loop on Windows
    udp_socket.settimeout(1)

    Thread(target=tunnel_loop, daemon=True).start()
    Thread(target=heartbeat, daemon=True).start()

    print(f'Listening for UDP on {LOCAL_IP}:{LOCAL_UDP_PORT}')
    try:
        while True:
            receive_in(udp_socket)
    except KeyboardInterrupt:
        pass


main()
