# udp2tcp2udp
Client/Server script for forwarding UDP traffic over a TCP tunnel.

These scripts were written to fill a niche use case that I had where I needed to forward traffic to and from a service that only listens on a UDP port over an SSH tunnel. (In this case, a Minecraft Bedrock server.) The only other solutions I could find for this problem would only send the traffic one way, but I needed a bidirectional solution.

Effectively, these scripts will take in UDP packets, forward them over a TCP connection, and then forward the data to another UDP port on the other end. Any replies from the final destination to the sending UDP port will then be forwarded back over the connection. This can be useful if you need to send UDP traffic over an SSH tunnel, or if you are dealing with a firewall that only allows TCP traffic.

A basic setup would be as follows:

Client software <===UDP===> System running client.py <===TCP===> System running server.py <===UDP===> Service listening on UDP.

This has only been tested on a Minecraft server, but I would assume these scripts could also be used for any other service that listens on UDP (DNS, for example).

SETUP

Define destination service address and port in "server.py"
Define server.py system IP and port in client.py

Run server.py on server system.

```python3 server.py```

Run client.py on client system.

```python3 client.py```
