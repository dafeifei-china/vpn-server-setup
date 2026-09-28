#!/bin/sh
set -e
sysctl -w net.ipv4.ip_forward=1
iptables -t nat -C POSTROUTING -s 10.99.99.0/24 ! -d 10.99.99.0/24 -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -s 10.99.99.0/24 ! -d 10.99.99.0/24 -j MASQUERADE
iptables -C FORWARD -s 10.99.99.0/24 -p tcp \
  -m tcp --tcp-flags FIN,SYN,RST,ACK SYN -j TCPMSS --set-mss 1356 2>/dev/null \
  || iptables -A FORWARD -s 10.99.99.0/24 -p tcp \
  -m tcp --tcp-flags FIN,SYN,RST,ACK SYN -j TCPMSS --set-mss 1356
exec "$@"
