#!/bin/sh
set -e
VPN_SUBNET="${VPN_SUBNET:-192.168.45.0/24}"

# PPTP 用 GRE(proto 47)；为 NAT 后的客户端加载 conntrack 辅助模块，MPPE 需要 ppp_mppe
modprobe nf_conntrack_pptp 2>/dev/null || true
modprobe nf_nat_pptp       2>/dev/null || true
modprobe ppp_mppe          2>/dev/null || true

sysctl -w net.ipv4.ip_forward=1
sysctl -w net.ipv4.conf.all.accept_redirects=0 2>/dev/null || true
sysctl -w net.ipv4.conf.all.send_redirects=0 2>/dev/null || true

iptables -t nat -C POSTROUTING -s "$VPN_SUBNET" ! -d "$VPN_SUBNET" -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -s "$VPN_SUBNET" ! -d "$VPN_SUBNET" -j MASQUERADE
iptables -C FORWARD -s "$VPN_SUBNET" -j ACCEPT 2>/dev/null \
  || iptables -A FORWARD -s "$VPN_SUBNET" -j ACCEPT
iptables -C FORWARD -d "$VPN_SUBNET" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null \
  || iptables -A FORWARD -d "$VPN_SUBNET" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
iptables -C FORWARD -s "$VPN_SUBNET" -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null \
  || iptables -A FORWARD -s "$VPN_SUBNET" -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu

exec /usr/sbin/pptpd -f
