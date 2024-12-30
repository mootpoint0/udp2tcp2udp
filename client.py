import socket
from threading import Thread

LOCAL_IP = '0.0.0.0'
LOCAL_UDP_PORT = 4667
LOCAL_TCP_PORT = 4667

SERVER_IP = '127.0.0.1'
SERVER_PORT = 4668

udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

tcp_socket = socket.socket()

sessions = []

def receive_out(r_socket,number):
    while True:
        message = r_socket.recv(4096)

        decoded_data = message.decode()

        handle_out_message(decoded_data)

def handle_out_message(decoded_data):
    if '$' not in decoded_data:
        return 0
    packet_split = decoded_data.split('$',1)
    decoded_data = packet_split[0]
    if len(packet_split)>1:
        handle_out_message(packet_split[1])
    if '-' not in decoded_data:
        return 0
    data_split = decoded_data.split('-',2)

    if data_split[0]+data_split[1] not in sessions:
            return 0

    client_ip = data_split[0]
    client_port = int(data_split[1])
    forward_data = data_split[2]

    #print("Data forwarded: ")
    #print(forward_data+"$ Was forwarded")
    #print("End")

    byte_data = bytearray.fromhex(forward_data)

    respond(client_port,client_ip,byte_data)

    return 0

def forward(f_socket, data, address):
    f_socket.send(data)
    

def respond(client_port, client_ip, data):
    udp_socket.sendto(data,(client_ip,client_port))

    return 0

def receive_in(r_socket):
    data, address = r_socket.recvfrom(4096)

    source_ip = address[0]
    source_port = address[1]

    sessions.append(address[0]+str(address[1]))

    message = data.hex()

    new_message = str(source_ip)+"-"+str(source_port)+"-"+message+'$'

    new_data = new_message.encode()

    #print(new_message)

    forward(tcp_socket,new_data,SERVER_IP)

    return 0

def main():
    udp_socket.bind((LOCAL_IP, LOCAL_UDP_PORT))
    tcp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp_socket.bind((LOCAL_IP, LOCAL_TCP_PORT))
    tcp_socket.connect((SERVER_IP,SERVER_PORT))

    r_out_thread = Thread(target=receive_out, args=(tcp_socket,0))
    r_out_thread.start()

    while True:
        receive_in(udp_socket)

main()
