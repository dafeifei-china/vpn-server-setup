#!/bin/sh
set -e
VPN_POOL="${VPN_POOL:-10.10.10.0/24}"

sysctl -w net.ipv4.ip_forward=1
sysctl -w net.ipv4.conf.all.accept_redirects=0 2>/dev/null || true
sysctl -w net.ipv4.conf.all.send_redirects=0 2>/dev/null || true

iptables -t nat -C POSTROUTING -s "$VPN_POOL" ! -d "$VPN_POOL" -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -s "$VPN_POOL" ! -d "$VPN_POOL" -j MASQUERADE
iptables -C FORWARD -s "$VPN_POOL" -j ACCEPT 2>/dev/null \
  || iptables -A FORWARD -s "$VPN_POOL" -j ACCEPT
iptables -C FORWARD -d "$VPN_POOL" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null \
  || iptables -A FORWARD -d "$VPN_POOL" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
iptables -C FORWARD -s "$VPN_POOL" -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null \
  || iptables -A FORWARD -s "$VPN_POOL" -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu

exec ipsec start --nofork
