#!/usr/bin/env bash
#
# setup-openconnect.sh — 基于 Docker 部署 OpenConnect (ocserv) VPN
#
# 协议：Cisco AnyConnect / OpenConnect（TLS + DTLS），默认端口 8443
#
# 用法：
#   ./setup-openconnect.sh
#   SERVER_IP=192.168.0.106 ./setup-openconnect.sh
#   PORT=443 ./setup-openconnect.sh
#
# 说明：
#   - 容器用桥接 + 端口映射，VPN 客户端流量在容器内做 NAT
#   - 需要 /dev/net/tun 与 NET_ADMIN（用 --privileged 简化）
#   - 隧道网段默认 192.168.46.0/24，与其它 VPN 均不冲突
#   - 自签服务端证书（CN/SAN = 服务器 IP）；账号 test/test（plain 认证）
#   - 可重复运行：重建镜像与容器，重签证书、重建账号
#
set -euo pipefail

########################  配置区  ########################
SERVER_IP="${SERVER_IP:-}"
VPN_SUBNET="${VPN_SUBNET:-192.168.46.0}"
VPN_NETMASK="${VPN_NETMASK:-255.255.255.0}"
USER_NAME="${USER_NAME:-test}"
USER_PASS="${USER_PASS:-test}"
DNS1="${DNS1:-8.8.8.8}"
DNS2="${DNS2:-8.8.4.4}"
PORT="${PORT:-8443}"
CERT_CN="${CERT_CN:-$SERVER_IP}"          # 证书 CN，默认服务器 IP
IMAGE="${IMAGE:-openconnect-vpn}"
CONTAINER="${CONTAINER:-openconnect-server}"
BASE_IMAGE="${BASE_IMAGE:-ubuntu:20.04}"
#########################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR"
OCSERV_DIR="$BUILD_DIR/etc/ocserv"

log()  { printf '\033[1;32m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
dk() { $SUDO docker "$@"; }

# ------------------------------------------------------------------ #
# 0. 环境 / IP
# ------------------------------------------------------------------ #
command -v docker >/dev/null 2>&1 || die "未找到 docker，请先安装"
$SUDO docker info >/dev/null 2>&1 || die "Docker 未运行：systemctl start docker"
[ -c /dev/net/tun ] || die "缺少 /dev/net/tun（请加载 tun 模块：sudo modprobe tun）"

if [ -z "$SERVER_IP" ]; then
  SERVER_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
  [ -n "$SERVER_IP" ] || die "无法自动探测 IP，请用 SERVER_IP=x.x.x.x 运行"
  log "自动探测到 SERVER_IP=$SERVER_IP"
else
  log "使用指定 SERVER_IP=$SERVER_IP"
fi
[ "$CERT_CN" = "$SERVER_IP" ] || : # CERT_CN 已在上面默认

# ------------------------------------------------------------------ #
# 1. 生成构建文件
# ------------------------------------------------------------------ #
mkdir -p "$OCSERV_DIR"

cat > "$BUILD_DIR/Dockerfile" <<EOF
FROM $BASE_IMAGE

ENV DEBIAN_FRONTEND noninteractive
RUN sed -i 's|//archive.ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g; \\
           s|//security.ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list \\
 && apt-get update \\
 && apt-get install -y ocserv iptables iproute2 kmod \\
 && rm -rf /var/lib/apt/lists/*

COPY etc/ocserv/ /etc/ocserv/
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0700 /entrypoint.sh

EXPOSE $PORT/tcp $PORT/udp
ENTRYPOINT ["/entrypoint.sh"]
EOF

cat > "$BUILD_DIR/entrypoint.sh" <<'EOF'
#!/bin/sh
set -e
VPN_SUBNET="${VPN_SUBNET:-192.168.46.0/24}"

modprobe tun 2>/dev/null || true

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

exec /usr/sbin/ocserv --foreground --pid-file /var/run/ocserv.pid --config /etc/ocserv/ocserv.conf
EOF
chmod +x "$BUILD_DIR/entrypoint.sh"

cat > "$OCSERV_DIR/ocserv.conf" <<EOF
# ocserv 主配置（由 setup-openconnect.sh 生成）
auth = "certificate"

tcp-port = $PORT
udp-port = $PORT

run-as-user = nobody
run-as-group = nogroup
socket-file = /var/run/ocserv-socket

server-cert = /etc/ocserv/server.crt
server-key  = /etc/ocserv/server.key
ca-cert     = /etc/ocserv/ca.crt

isolate-workers = true
max-clients = 16
max-same-clients = 2
keepalive = 32400
dpd = 90
mobile-dpd = 1800
switch-to-tcp-timeout = 25
try-mtu-discovery = true
cert-user-oid = 2.5.4.3
tls-priorities = "NORMAL:%SERVER_PRECEDENCE:%COMPAT:-VERS-SSL3.0"
auth-timeout = 240
min-reauth-time = 300
max-ban-score = 80
ban-reset-time = 1200
cookie-timeout = 300
deny-roaming = false
rekey-time = 172800
rekey-method = ssl
use-occtl = true
pid-file = /var/run/ocserv.pid

device = vpns
predictable-ips = true
default-domain = example.com
ipv4-network = $VPN_SUBNET
ipv4-netmask = $VPN_NETMASK
dns = $DNS1
dns = $DNS2
ping-leases = false
route = default
no-route = $VPN_SUBNET/$VPN_NETMASK
cisco-client-compat = true
dtls-legacy = true
EOF

# ------------------------------------------------------------------ #
# 2. 证书（证书认证）：CA + 服务端（CN/SAN = 服务器 IP）+ 客户端（CN = 用户名）
# ------------------------------------------------------------------ #
# CA：首次生成，之后复用（换 IP 只重签服务端证书，不必换 CA）
if [ ! -f "$OCSERV_DIR/ca.crt" ]; then
  log "生成 CA ..."
  openssl genrsa -out "$OCSERV_DIR/ca.key" 4096 >/dev/null 2>&1
  openssl req -x509 -new -nodes -key "$OCSERV_DIR/ca.key" -sha256 -days 3650 \
    -subj "/C=CN/O=OpenConnect VPN/CN=OpenConnect VPN CA" -out "$OCSERV_DIR/ca.crt"
fi

log "生成服务端证书（CA 签发，CN=$CERT_CN, SAN=$SERVER_IP）..."
openssl genrsa -out "$OCSERV_DIR/server.key" 2048 >/dev/null 2>&1
openssl req -new -key "$OCSERV_DIR/server.key" \
  -subj "/C=CN/O=OpenConnect VPN/CN=$CERT_CN" -out "$OCSERV_DIR/server.csr" 2>/dev/null
openssl x509 -req -in "$OCSERV_DIR/server.csr" -CA "$OCSERV_DIR/ca.crt" -CAkey "$OCSERV_DIR/ca.key" \
  -CAcreateserial -days 3650 -sha256 -out "$OCSERV_DIR/server.crt" \
  -extfile <(printf "subjectAltName=IP:%s\nextendedKeyUsage=serverAuth\nkeyUsage=digitalSignature,keyEncipherment" "$SERVER_IP") 2>/dev/null
rm -f "$OCSERV_DIR/server.csr"

# 客户端证书：CN 即登录名（cert-user-oid = 2.5.4.3）
if [ ! -f "$OCSERV_DIR/client.crt" ]; then
  log "生成客户端证书（CN=$USER_NAME）..."
  openssl genrsa -out "$OCSERV_DIR/client.key" 2048 >/dev/null 2>&1
  openssl req -new -key "$OCSERV_DIR/client.key" \
    -subj "/C=CN/O=OpenConnect VPN/CN=$USER_NAME" -out "$OCSERV_DIR/client.csr" 2>/dev/null
  openssl x509 -req -in "$OCSERV_DIR/client.csr" -CA "$OCSERV_DIR/ca.crt" -CAkey "$OCSERV_DIR/ca.key" \
    -CAcreateserial -days 3650 -sha256 -out "$OCSERV_DIR/client.crt" \
    -extfile <(printf "extendedKeyUsage=clientAuth\nkeyUsage=digitalSignature,keyEncipherment") 2>/dev/null
  rm -f "$OCSERV_DIR/client.csr"
  openssl pkcs12 -export -inkey "$OCSERV_DIR/client.key" -in "$OCSERV_DIR/client.crt" \
    -certfile "$OCSERV_DIR/ca.crt" -out "$OCSERV_DIR/client.p12" -passout pass: 2>/dev/null
fi
chmod 600 "$OCSERV_DIR/ca.key" "$OCSERV_DIR/server.key" "$OCSERV_DIR/client.key" "$OCSERV_DIR/client.p12" 2>/dev/null || true

# ------------------------------------------------------------------ #
# 3. 账号（plain 认证，SHA-512 crypt）
# ------------------------------------------------------------------ #
log "生成账号 $USER_NAME ..."
HASH="$(openssl passwd -6 -salt "$(openssl rand -hex 4)" "$USER_PASS")"
printf '%s:%s:\n' "$USER_NAME" "$HASH" > "$OCSERV_DIR/ocpasswd"
chmod 600 "$OCSERV_DIR/ocpasswd"

# ------------------------------------------------------------------ #
# 4. 构建镜像
# ------------------------------------------------------------------ #
log "构建镜像 $IMAGE ..."
dk build -t "$IMAGE" "$BUILD_DIR"

# ------------------------------------------------------------------ #
# 5. 重建容器
# ------------------------------------------------------------------ #
if dk ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  log "移除旧容器 $CONTAINER"
  dk rm -f "$CONTAINER" >/dev/null
fi

log "启动 OpenConnect 容器 $CONTAINER ..."
dk run -d --name "$CONTAINER" --restart=unless-stopped \
  --privileged \
  -v /dev/net/tun:/dev/net/tun \
  -p "$PORT:$PORT/tcp" \
  -p "$PORT:$PORT/udp" \
  -e VPN_SUBNET="$VPN_SUBNET/24" \
  "$IMAGE" >/dev/null

sleep 3
if dk ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  log "容器已启动"
else
  warn "容器未运行，请查看：$SUDO docker logs $CONTAINER"
fi

# ------------------------------------------------------------------ #
# 6. 校验
# ------------------------------------------------------------------ #
echo "------------------------------------------------------------------"
if ss -tlnp 2>/dev/null | grep -q ":$PORT "; then
  log "监听 $PORT/tcp ✓"
else
  warn "未发现 $PORT/tcp 监听"
fi
dk logs --tail 20 "$CONTAINER" 2>&1 | grep -iE "ocserv|listening|error" | tail -5 || true

# ------------------------------------------------------------------ #
# 7. 输出连接信息
# ------------------------------------------------------------------ #
cat <<EOF
==================================================================
 OpenConnect (ocserv) VPN 已就绪
------------------------------------------------------------------
 服务器地址   : $SERVER_IP
 端口         : $PORT/tcp（TLS）+ $PORT/udp（DTLS）
 认证方式     : 证书（certificate），登录名取自客户端证书 CN
 VPN 网段     : $VPN_SUBNET $VPN_NETMASK
 CA 证书      : $OCSERV_DIR/ca.crt
 客户端证书   : $OCSERV_DIR/client.crt（CN=$USER_NAME）
 客户端私钥   : $OCSERV_DIR/client.key
 客户端 p12   : $OCSERV_DIR/client.p12
 容器         : $CONTAINER（桥接 + 端口映射）
------------------------------------------------------------------
 客户端：openconnect --certificate=client.crt --sslkey=client.key --cafile=ca.crt $SERVER_IP:$PORT
        NetworkManager 图形界面：网关 $SERVER_IP:$PORT；CA=ca.crt；用户证书=client.crt；私钥=client.key
 防火墙/安全组需放行 TCP+UDP $PORT。
------------------------------------------------------------------
 管理：
   $SUDO docker stop  $CONTAINER
   $SUDO docker start $CONTAINER
   $SUDO docker logs -f $CONTAINER
   $SUDO docker exec -it $CONTAINER occtl show users
==================================================================
EOF
