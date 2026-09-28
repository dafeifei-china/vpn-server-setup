#!/usr/bin/env bash
#
# setup-pptp.sh — 基于 Docker 部署 PPTP VPN（TCP 1723 + GRE proto 47）
#
# ⚠️ PPTP 早已被认为不安全（MS-CHAPv2 可被破解，MPPE 也很弱），仅用于兼容旧客户端/测试。
#    生产环境请改用 OpenVPN / strongSwan / L2TP-IPsec。
#
# 用法：
#   ./setup-pptp.sh
#   SERVER_IP=192.168.0.106 ./setup-pptp.sh
#   MPPE=no ./setup-pptp.sh            # 不要求 MPPE（默认 yes = require-mppe-128）
#
# 说明：
#   - PPTP 需要 GRE(协议 47)，Docker 桥接无法转发，故用 --net host --privileged
#   - 隧道网段默认 192.168.45.0/24，与 OpenVPN(10.99.99)、L2TP(192.168.42/44)、strongSwan(10.10.10) 均不冲突
#   - 容器内做 ip_forward + NAT + TCPMSS，并尝试加载 nf_conntrack_pptp/nf_nat_pptp/ppp_mppe
#   - 可重复运行：重建镜像与容器；账号在脚本变量里
#
set -euo pipefail

########################  配置区  ########################
SERVER_IP="${SERVER_IP:-}"
VPN_SUBNET="${VPN_SUBNET:-192.168.45.0}"
VPN_NETMASK="${VPN_NETMASK:-255.255.255.0}"
LOCAL_IP="${LOCAL_IP:-192.168.45.1}"
POOL_START="${POOL_START:-192.168.45.10}"
POOL_END="${POOL_END:-192.168.45.250}"
USER_NAME="${USER_NAME:-test}"
USER_PASS="${USER_PASS:-test}"
DNS1="${DNS1:-8.8.8.8}"
DNS2="${DNS2:-8.8.4.4}"
MPPE="${MPPE:-yes}"                 # yes = require-mppe-128；no = 不要求 MPPE
PORT="${PORT:-1723}"
IMAGE="${IMAGE:-pptp-vpn}"
CONTAINER="${CONTAINER:-pptp-server}"
BASE_IMAGE="${BASE_IMAGE:-ubuntu:20.04}"
OPENVPN_CONTAINER="${OPENVPN_CONTAINER:-running-vpn-openvpn-tls}"
#########################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR"

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

if [ -z "$SERVER_IP" ]; then
  SERVER_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
  [ -n "$SERVER_IP" ] || die "无法自动探测 IP，请用 SERVER_IP=x.x.x.x 运行"
  log "自动探测到 SERVER_IP=$SERVER_IP"
else
  log "使用指定 SERVER_IP=$SERVER_IP"
fi

# ------------------------------------------------------------------ #
# 1. 生成构建文件
# ------------------------------------------------------------------ #
mkdir -p "$BUILD_DIR/etc/ppp"

cat > "$BUILD_DIR/Dockerfile" <<EOF
FROM $BASE_IMAGE

ENV DEBIAN_FRONTEND noninteractive
RUN sed -i 's|//archive.ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g; \\
           s|//security.ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list \\
 && apt-get update \\
 && apt-get install -y pptpd iptables iproute2 kmod \\
 && rm -rf /var/lib/apt/lists/*

COPY etc/pptpd.conf /etc/pptpd.conf
COPY etc/ppp/pptpd-options /etc/ppp/pptpd-options
COPY etc/ppp/chap-secrets /etc/ppp/chap-secrets
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0700 /entrypoint.sh

EXPOSE $PORT/tcp
ENTRYPOINT ["/entrypoint.sh"]
EOF

cat > "$BUILD_DIR/entrypoint.sh" <<'EOF'
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
EOF
chmod +x "$BUILD_DIR/entrypoint.sh"

cat > "$BUILD_DIR/etc/pptpd.conf" <<EOF
option /etc/ppp/pptpd-options
logwtmp
localip $LOCAL_IP
remoteip $POOL_START-${POOL_END##*.}
EOF

MPPE_OPT=""
case "$MPPE" in
  [yY][eE][sS]) MPPE_OPT="require-mppe-128" ;;
esac
cat > "$BUILD_DIR/etc/ppp/pptpd-options" <<EOF
name pptpd
refuse-pap
refuse-chap
refuse-mschap
require-mschap-v2
$MPPE_OPT
ms-dns $DNS1
ms-dns $DNS2
proxyarp
lock
nobsdcomp
novj
novjccomp
nologfd
mtu 1400
mru 1400
EOF

cat > "$BUILD_DIR/etc/ppp/chap-secrets" <<EOF
# client  server  secret  IP addresses
$USER_NAME pptpd $USER_PASS *
EOF
chmod 600 "$BUILD_DIR/etc/ppp/chap-secrets"

# ------------------------------------------------------------------ #
# 2. 构建镜像
# ------------------------------------------------------------------ #
log "构建镜像 $IMAGE ..."
dk build -t "$IMAGE" "$BUILD_DIR"

# ------------------------------------------------------------------ #
# 3. 重建容器（--net host，GRE 需要）
# ------------------------------------------------------------------ #
if dk ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  log "移除旧容器 $CONTAINER"
  dk rm -f "$CONTAINER" >/dev/null
fi

log "启动 PPTP 容器 $CONTAINER ..."
dk run -d --name "$CONTAINER" --restart=unless-stopped \
  --net host --privileged \
  -v /lib/modules:/lib/modules:ro \
  -e VPN_SUBNET="$VPN_SUBNET/24" \
  "$IMAGE" >/dev/null

sleep 3
if dk ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  log "容器已启动"
else
  warn "容器未运行，请查看：$SUDO docker logs $CONTAINER"
fi

# ------------------------------------------------------------------ #
# 4. 校验
# ------------------------------------------------------------------ #
echo "------------------------------------------------------------------"
if ss -tlnp 2>/dev/null | grep -q ":$PORT "; then
  log "监听 $PORT/tcp ✓"
else
  warn "未发现 $PORT/tcp 监听"
fi
if dk ps --format '{{.Names}}' | grep -qx "$OPENVPN_CONTAINER"; then
  log "OpenVPN 容器仍在运行 ✓"
fi
dk logs --tail 20 "$CONTAINER" 2>&1 | grep -iE "pptpd|started|listening" | tail -5 || true

# ------------------------------------------------------------------ #
# 5. 输出连接信息
# ------------------------------------------------------------------ #
cat <<EOF
==================================================================
 PPTP VPN 已就绪
------------------------------------------------------------------
 服务器地址   : $SERVER_IP
 端口         : $PORT/tcp + GRE(协议 47)
 用户名/密码  : $USER_NAME / $USER_PASS
 VPN 网段     : $VPN_SUBNET $VPN_NETMASK（服务端 $LOCAL_IP）
 MPPE         : $MPPE
 容器         : $CONTAINER（--net host --privileged）
------------------------------------------------------------------
 客户端：新建 PPTP 类型 VPN，服务器填 $SERVER_IP，账号 $USER_NAME/$USER_PASS。
 防火墙/安全组需放行 TCP $PORT 与 GRE(协议 47)。
------------------------------------------------------------------
 管理：
   $SUDO docker stop  $CONTAINER
   $SUDO docker start $CONTAINER
   $SUDO docker logs -f $CONTAINER
==================================================================
EOF
