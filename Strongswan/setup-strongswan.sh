#!/usr/bin/env bash
#
# setup-strongswan.sh — Docker 版 strongSwan IKEv2 VPN（证书认证）
#
# 用法：
#   ./setup-strongswan.sh
#   SERVER_IP=192.168.0.106 ./setup-strongswan.sh
#
# 说明：
#   - 需要 UDP 500/4500；若被 L2TP/IPsec 容器（ipsec-vpn-server）占用会自动停用它
#   - 采用 --net host --privileged（容器里跑 IPsec 的常规做法）
#   - VPN 网段默认 10.10.10.0/24，与 OpenVPN(10.99.99.0/24)、L2TP(192.168.42/44.0/24) 均不冲突
#   - 证书用容器自带 strongSwan pki 生成，存放在 pki/
#   - 可重复运行：已有证书复用，镜像重建、容器重建
#
set -euo pipefail

########################  配置区  ########################
SERVER_IP="${SERVER_IP:-}"
VPN_POOL="${VPN_POOL:-10.10.10.0/24}"
CA_CN="${CA_CN:-StrongSwan VPN CA}"
CA_O="${CA_O:-StrongSwan VPN}"
CLIENT_NAME="${CLIENT_NAME:-client1}"
DNS1="${DNS1:-8.8.8.8}"
DNS2="${DNS2:-8.8.4.4}"
IMAGE="${IMAGE:-strongswan-vpn}"
CONTAINER="${CONTAINER:-strongswan-server}"
BASE_IMAGE="${BASE_IMAGE:-ubuntu:20.04}"
L2TP_IPSEC_CONTAINER="${L2TP_IPSEC_CONTAINER:-ipsec-vpn-server}"
OPENVPN_CONTAINER="${OPENVPN_CONTAINER:-running-vpn-openvpn-tls}"
#########################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKI="$SCRIPT_DIR/pki"
CLIENTS="$SCRIPT_DIR/clients"

log()  { printf '\033[1;32m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] && SUDO="" || SUDO="sudo"
dk() { $SUDO docker "$@"; }

# ------------------------------------------------------------------ #
# 0. 环境 / IP
# ------------------------------------------------------------------ #
command -v docker >/dev/null 2>&1 || die "未找到 docker"
$SUDO docker info >/dev/null 2>&1 || die "Docker 未运行"

if [ -z "$SERVER_IP" ]; then
  SERVER_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
  [ -n "$SERVER_IP" ] || die "无法自动探测 IP，请用 SERVER_IP=x.x.x.x 运行"
  log "自动探测到 SERVER_IP=$SERVER_IP"
else
  log "使用指定 SERVER_IP=$SERVER_IP"
fi

# ------------------------------------------------------------------ #
# 1. 腾出 500/4500
# ------------------------------------------------------------------ #
if dk ps --format '{{.Names}}' | grep -qx "$L2TP_IPSEC_CONTAINER"; then
  warn "L2TP/IPsec 容器占用 500/4500，停用它让给 strongSwan（可用 docker start 恢复）"
  dk stop "$L2TP_IPSEC_CONTAINER" >/dev/null
fi

# ------------------------------------------------------------------ #
# 2. 生成构建文件
# ------------------------------------------------------------------ #
mkdir -p "$SCRIPT_DIR/etc" "$PKI/cacerts" "$PKI/certs" "$PKI/private" "$CLIENTS"

cat > "$SCRIPT_DIR/Dockerfile" <<EOF
FROM $BASE_IMAGE

ENV DEBIAN_FRONTEND noninteractive
RUN sed -i 's|//archive.ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g; \\
           s|//security.ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list \\
 && apt-get update \\
 && apt-get install -y \\
      strongswan strongswan-starter strongswan-swanctl strongswan-pki \\
      libcharon-extra-plugins iptables iproute2 \\
 && rm -rf /var/lib/apt/lists/*

COPY etc/ipsec.conf /etc/ipsec.conf
COPY etc/ipsec.secrets /etc/ipsec.secrets
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0700 /entrypoint.sh

EXPOSE 500/udp 4500/udp
ENTRYPOINT ["/entrypoint.sh"]
EOF

cat > "$SCRIPT_DIR/entrypoint.sh" <<'EOF'
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
EOF

cat > "$SCRIPT_DIR/etc/ipsec.conf" <<EOF
config setup
    charondebug="ike 1, knl 1, cfg 0"
    uniqueids=no

conn ikev2-cert
    auto=add
    type=tunnel
    keyexchange=ikev2
    fragmentation=yes
    forceencaps=yes
    dpdaction=clear
    dpddelay=300s
    rekey=no
    left=%any
    leftid=$SERVER_IP
    leftcert=server.crt
    leftsendcert=always
    leftsubnet=0.0.0.0/0
    right=%any
    rightid=%any
    rightauth=pubkey
    rightsourceip=$VPN_POOL
    rightdns=$DNS1,$DNS2
    rightsendcert=always
EOF

cat > "$SCRIPT_DIR/etc/ipsec.secrets" <<'EOF'
: RSA server.key
EOF

# ------------------------------------------------------------------ #
# 3. 构建镜像
# ------------------------------------------------------------------ #
log "构建镜像 $IMAGE ..."
dk build -t "$IMAGE" "$SCRIPT_DIR"

# ------------------------------------------------------------------ #
# 4. 证书（首次生成，之后复用）
# ------------------------------------------------------------------ #
if [ ! -f "$PKI/cacerts/ca.crt" ]; then
  log "生成 CA / 服务端 / $CLIENT_NAME 证书（10 年）..."
  dk run --rm --entrypoint /bin/sh -v "$PKI:/etc/ipsec.d" "$IMAGE" -c "
set -e
cd /etc/ipsec.d
pki --gen --type rsa --size 4096 --outform pem > private/ca.key
pki --self --ca --lifetime 3650 --in private/ca.key --dn 'C=CN, O=$CA_O, CN=$CA_CN' --outform pem > cacerts/ca.crt
pki --gen --type rsa --size 2048 --outform pem > private/server.key
pki --pub --in private/server.key --outform pem > /tmp/server.pub
pki --issue --lifetime 3650 --cacert cacerts/ca.crt --cakey private/ca.key \
  --dn 'C=CN, O=$CA_O, CN=$SERVER_IP' --san $SERVER_IP \
  --flag serverAuth --flag ikeIntermediate \
  --in /tmp/server.pub --outform pem > certs/server.crt
pki --gen --type rsa --size 2048 --outform pem > private/$CLIENT_NAME.key
pki --pub --in private/$CLIENT_NAME.key --outform pem > /tmp/client.pub
pki --issue --lifetime 3650 --cacert cacerts/ca.crt --cakey private/ca.key \
  --dn 'C=CN, O=$CA_O, CN=$CLIENT_NAME' --flag clientAuth \
  --in /tmp/client.pub --outform pem > certs/$CLIENT_NAME.crt
echo CERTS-OK
"
  dk run --rm --entrypoint /bin/sh -v "$PKI:/etc/ipsec.d" "$IMAGE" -c "chmod 600 /etc/ipsec.d/private/*.key" 2>/dev/null || true
else
  # 证书已存在：CA 与客户端证书复用。但服务端证书的 CN/SAN 绑定了 IP，
  # 换 IP 后必须重签服务端证书（保留 CA，客户端证书/文件无需更换）。
  if openssl x509 -in "$PKI/certs/server.crt" -noout -checkip "$SERVER_IP" >/dev/null 2>&1; then
    log "证书已存在，复用 $PKI（CA/客户端证书不变）"
  else
    log "服务端证书 IP 与 $SERVER_IP 不符，重签服务端证书（保留 CA 与客户端证书）..."
    dk run --rm --entrypoint /bin/sh -v "$PKI:/etc/ipsec.d" "$IMAGE" -c "
set -e
cd /etc/ipsec.d
pki --pub --in private/server.key --outform pem > /tmp/server.pub
pki --issue --lifetime 3650 --cacert cacerts/ca.crt --cakey private/ca.key \
  --dn 'C=CN, O=$CA_O, CN=$SERVER_IP' --san $SERVER_IP \
  --flag serverAuth --flag ikeIntermediate \
  --in /tmp/server.pub --outform pem > certs/server.crt
echo SERVER-CERT-REISSUED
"
  fi
fi

# ------------------------------------------------------------------ #
# 5. 客户端 profile
# ------------------------------------------------------------------ #
log "生成客户端 profile 到 $CLIENTS"
cp "$PKI/cacerts/ca.crt" "$CLIENTS/ca.crt"
cp "$PKI/certs/$CLIENT_NAME.crt" "$CLIENTS/$CLIENT_NAME.crt"
openssl pkcs12 -export -inkey "$PKI/private/$CLIENT_NAME.key" -in "$PKI/certs/$CLIENT_NAME.crt" \
  -certfile "$PKI/cacerts/ca.crt" -name "$CLIENT_NAME" \
  -out "$CLIENTS/$CLIENT_NAME.p12" -passout pass:
chmod 600 "$CLIENTS/$CLIENT_NAME.p12"
cp "$PKI/private/$CLIENT_NAME.key" "$CLIENTS/$CLIENT_NAME.key"
chmod 600 "$CLIENTS/$CLIENT_NAME.key"

cat > "$CLIENTS/strongswan-client.conf" <<EOF
conn ikev2-client
    keyexchange=ikev2
    right=$SERVER_IP
    rightid=$SERVER_IP
    rightsubnet=0.0.0.0/0
    left=%defaultroute
    leftcert=$CLIENT_NAME.crt
    leftid=@$CLIENT_NAME
    leftsendcert=always
    leftsourceip=%config
    auto=add
EOF

# 属主还给调用者（容器以 root 写的）
[ -n "$SUDO" ] && $SUDO chown -R "${SUDO_USER:-$(id -un)}" "$PKI" "$CLIENTS" 2>/dev/null || true

# ------------------------------------------------------------------ #
# 6. 启动容器
# ------------------------------------------------------------------ #
if dk ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  log "移除旧容器 $CONTAINER"
  dk rm -f "$CONTAINER" >/dev/null
fi

log "启动容器 $CONTAINER ..."
dk run -d --name "$CONTAINER" --net host --privileged --restart unless-stopped \
  -e VPN_POOL="$VPN_POOL" \
  -v "$PKI:/etc/ipsec.d" \
  -v "$SCRIPT_DIR/etc/ipsec.conf:/etc/ipsec.conf:ro" \
  -v "$SCRIPT_DIR/etc/ipsec.secrets:/etc/ipsec.secrets:ro" \
  "$IMAGE" >/dev/null

sleep 6
if dk logs --tail 30 "$CONTAINER" 2>&1 | grep -q "charon .* started"; then
  log "strongSwan 已启动"
else
  warn "未检测到 charon 启动，请查看：$SUDO docker logs $CONTAINER"
fi

# ------------------------------------------------------------------ #
# 7. 校验
# ------------------------------------------------------------------ #
echo "------------------------------------------------------------------"
if ss -ulnp 2>/dev/null | grep -q ':500 '; then log "监听 500/4500 ✓"; else warn "未监听 500"; fi
if dk ps --format '{{.Names}}' | grep -qx "$OPENVPN_CONTAINER"; then log "OpenVPN 容器仍在运行 ✓"; fi
dk exec "$CONTAINER" ipsec statusall 2>/dev/null | grep -q "ikev2-cert" && log "连接 ikev2-cert 已加载 ✓"

cat <<EOF
==================================================================
 strongSwan IKEv2 VPN 已就绪
------------------------------------------------------------------
 服务器地址   : $SERVER_IP
 VPN 网段     : $VPN_POOL
 客户端文件   : $CLIENTS
   $CLIENT_NAME.p12             （导入 Windows/macOS/Android/Linux）
   ca.crt / $CLIENT_NAME.crt
   strongswan-client.conf       （Linux strongSwan 命令行）
 注意：UDP 500/4500 已被 strongSwan 占用，L2TP/IPsec 需保持停用。
------------------------------------------------------------------
 管理：
   $SUDO docker stop  $CONTAINER
   $SUDO docker start $CONTAINER
   $SUDO docker logs -f $CONTAINER
   $SUDO docker exec -it $CONTAINER ipsec statusall
==================================================================
EOF
