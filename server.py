import socket
from threading import Thread

LOCAL_IP = "0.0.0.0"
LOCAL_TCP_PORT = 4668

REMOTE_UDP_IP = '10.0.0.8'
REMOTE_UDP_PORT = 2667

tcp_sock = socket.socket()

client_addresses = {}
client_sockets = {}

def forward(data, connection):
    if '$' not in data:
        return 0
    packet_split = data.split('$',1)
    decoded_data = packet_split[0]
    if len(packet_split)>1:
      forward(packet_split[1],connection)


    if "-" not in decoded_data:
        return 0
    data_split = decoded_data.split('-',2)
    client_ip = data_split[0]
    client_port = data_split[1]
    forward_data = data_split[2]

    #print(client_ip)
    #print(client_port)
    #print(forward_data)

    client_key = client_ip + "-" + client_port

    make_thread = False

    if client_key not in client_sockets.keys():
        udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_socket.bind((LOCAL_IP, 0))

        port = udp_socket.getsockname()[1]

        client_addresses[port] = client_key

        client_sockets[client_key] = udp_socket
    
        make_thread = True
    else:
        udp_socket = client_sockets[client_key]
        port = udp_socket.getsockname()[1]

    byte_data = bytearray.fromhex(forward_data)

    #print(byte_data)

    #print(port)

    if make_thread:
        r_out_thread = Thread(target=receive_out, args=(udp_socket, client_ip, client_port, connection))
        r_out_thread.start()

    udp_socket.sendto(byte_data, (REMOTE_UDP_IP,REMOTE_UDP_PORT))

def reply():
    return 0

def receive_in(r_socket):
    connection, address = r_socket.accept()
    while True:
        data = connection.recv(4096)

        if not data: break

        #print(data)

        decoded_data = data.decode()

        forward(decoded_data, connection)

def receive_out(udp_socket, source_ip, source_port, connection):
    #udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    #port = int(source_port)
    #udp_socket.bind(('0.0.0.0', port))
    while True:
        data, address = udp_socket.recvfrom(4096)

        message = data.hex()

        new_message = str(source_ip)+"-"+str(source_port)+"-"+message+'$'

        new_data = new_message.encode()

        #print("reply: "+new_message)

        respond(connection,new_data)


def respond(connection,new_data):
    connection.send(new_data)
    return 0

def main():
    tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp_sock.bind((LOCAL_IP, LOCAL_TCP_PORT))
    tcp_sock.listen(5)
    receive_in(tcp_sock)

main()
