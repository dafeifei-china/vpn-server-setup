#!/usr/bin/env bash
#
# setup-vpnc.sh — 基于 Docker 部署 Cisco 兼容 IPSec VPN 服务端
#                 （供 vpnc / network-manager-vpnc 客户端连接）
#
# 协议：IKEv1 主动模式(aggressive) + PSK(组密钥) + XAUTH(用户名/密码) + MODECFG(下发 IP/DNS)
#
# 用法：
#   ./setup-vpnc.sh
#   SERVER_IP=192.168.0.106 ./setup-vpnc.sh
#   USER_NAME=xiong USER_PASS=xxx VPN_PSK=yyy ./setup-vpnc.sh
#
# 说明：
#   - 需要 UDP 500/4500；若被 strongswan-server / ipsec-vpn-server 占用会自动停用
#   - 采用 --net host --privileged（容器里跑 IPsec 的常规做法）
#   - VPN 网段默认 192.168.47.0/24，与其它 VPN 均不冲突
#   - 账号 test/test，组密钥(PSK) test；可重复运行：重建镜像与容器
#
set -euo pipefail

########################  配置区  ########################
SERVER_IP="${SERVER_IP:-}"
VPN_SUBNET="${VPN_SUBNET:-192.168.47.0}"          # 隧道网段（NAT 用）
VPN_NETMASK="${VPN_NETMASK:-255.255.255.0}"
VPN_POOL="${VPN_POOL:-192.168.47.10-192.168.47.250}"  # 分配给客户端的地址范围
USER_NAME="${USER_NAME:-test}"
USER_PASS="${USER_PASS:-test}"
VPN_PSK="${VPN_PSK:-test}"
DNS1="${DNS1:-8.8.8.8}"
DNS2="${DNS2:-8.8.4.4}"
IMAGE="${IMAGE:-vpnc-vpn}"
CONTAINER="${CONTAINER:-vpnc-server}"
BASE_IMAGE="${BASE_IMAGE:-ubuntu:20.04}"
STRONGSWAN_CONTAINER="${STRONGSWAN_CONTAINER:-strongswan-server}"
L2TP_IPSEC_CONTAINER="${L2TP_IPSEC_CONTAINER:-ipsec-vpn-server}"
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
# 1. 腾出 500/4500
# ------------------------------------------------------------------ #
for c in "$STRONGSWAN_CONTAINER" "$L2TP_IPSEC_CONTAINER"; do
  if dk ps --format '{{.Names}}' | grep -qx "$c"; then
    warn "$c 占用 500/4500，停用它让给 VPNC（可用 docker start 恢复）"
    dk stop "$c" >/dev/null
  fi
done

# ------------------------------------------------------------------ #
# 2. 生成构建文件
# ------------------------------------------------------------------ #
mkdir -p "$BUILD_DIR/etc" "$BUILD_DIR/clients"

cat > "$BUILD_DIR/Dockerfile" <<EOF
FROM $BASE_IMAGE

ENV DEBIAN_FRONTEND noninteractive
RUN sed -i 's|//archive.ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g; \\
           s|//security.ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list \\
 && apt-get update \\
 && apt-get install -y \\
      strongswan strongswan-starter libcharon-extra-plugins iptables iproute2 kmod \\
 && rm -rf /var/lib/apt/lists/*

COPY etc/ipsec.conf /etc/ipsec.conf
COPY etc/ipsec.secrets /etc/ipsec.secrets
COPY etc/strongswan-vpnc.conf /etc/strongswan.d/vpnc.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0700 /entrypoint.sh

EXPOSE 500/udp 4500/udp
ENTRYPOINT ["/entrypoint.sh"]
EOF

cat > "$BUILD_DIR/entrypoint.sh" <<'EOF'
#!/bin/sh
set -e
VPN_SUBNET="${VPN_SUBNET:-192.168.47.0/24}"

modprobe af_key 2>/dev/null || true

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

exec ipsec start --nofork
EOF
chmod +x "$BUILD_DIR/entrypoint.sh"

cat > "$BUILD_DIR/etc/ipsec.conf" <<EOF
config setup
    charondebug="ike 1, knl 1, cfg 1"
    uniqueids=yes

conn %default
    keyexchange=ikev1
    fragmentation=yes
    forceencaps=yes
    dpdaction=clear
    dpddelay=300s
    rekey=no
    ikelifetime=8h
    lifetime=1h

conn cisco-vpnc
    aggressive=yes
    authby=xauthpsk
    xauth=server
    left=%any
    leftsubnet=0.0.0.0/0
    right=%any
    rightsubnet=0.0.0.0/0
    rightsourceip=$VPN_POOL
    rightdns=$DNS1,$DNS2
    ike=aes256-sha1-modp1024,aes128-sha1-modp1024,3des-sha1-modp1024,aes128-md5-modp1024,3des-md5-modp1024
    esp=aes256-sha1,aes128-sha1,3des-sha1,aes128-md5,3des-md5
    auto=add
EOF

cat > "$BUILD_DIR/etc/ipsec.secrets" <<EOF
: PSK "$VPN_PSK"
$USER_NAME : XAUTH "$USER_PASS"
EOF
chmod 600 "$BUILD_DIR/etc/ipsec.secrets"

# strongSwan 5.8+ 默认禁用 Aggressive Mode + PSK，必须显式打开；顺带开文件日志便于排查
cat > "$BUILD_DIR/etc/strongswan-vpnc.conf" <<'EOF'
charon {
    i_dont_care_about_security_and_use_aggressive_mode_psk = yes
    filelog {
        vpnc {
            path = /var/log/charon.log
            default = 1
            ike = 1
            flush_line = yes
        }
    }
}
EOF

# 客户端示例配置
cat > "$BUILD_DIR/clients/vpnc.conf" <<EOF
# vpnc 客户端配置（放到 /etc/vpnc/<名字>.conf 或 /etc/vpnc/default.conf）
# 用法： sudo vpnc /etc/vpnc/vpnc.conf
IPSec gateway $SERVER_IP
IPSec ID     $USER_NAME
IPSec secret $VPN_PSK
Xauth username $USER_NAME
Xauth password $USER_PASS
IKE Authmode psk
EOF

# ------------------------------------------------------------------ #
# 3. 构建镜像
# ------------------------------------------------------------------ #
log "构建镜像 $IMAGE ..."
dk build -t "$IMAGE" "$BUILD_DIR"

# ------------------------------------------------------------------ #
# 4. 重建容器
# ------------------------------------------------------------------ #
if dk ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  log "移除旧容器 $CONTAINER"
  dk rm -f "$CONTAINER" >/dev/null
fi

log "启动 VPNC 容器 $CONTAINER ..."
dk run -d --name "$CONTAINER" --net host --privileged --restart unless-stopped \
  -v /lib/modules:/lib/modules:ro \
  -e VPN_SUBNET="$VPN_SUBNET/24" \
  "$IMAGE" >/dev/null

sleep 6
if dk logs --tail 30 "$CONTAINER" 2>&1 | grep -q "charon .* started"; then
  log "strongSwan(VPnC) 已启动"
else
  warn "未检测到 charon 启动，请查看：$SUDO docker logs $CONTAINER"
fi

# ------------------------------------------------------------------ #
# 5. 校验
# ------------------------------------------------------------------ #
echo "------------------------------------------------------------------"
if ss -ulnp 2>/dev/null | grep -q ':500 '; then log "监听 500/4500 ✓"; else warn "未监听 500"; fi
dk exec "$CONTAINER" ipsec statusall 2>/dev/null | grep -q "cisco-vpnc" && log "连接 cisco-vpnc 已加载 ✓" || warn "未加载 cisco-vpnc 连接"
if dk ps --format '{{.Names}}' | grep -qx "$OPENVPN_CONTAINER"; then log "OpenVPN 容器仍在运行 ✓"; fi

# ------------------------------------------------------------------ #
# 6. 输出连接信息
# ------------------------------------------------------------------ #
cat <<EOF
==================================================================
 VPNC (Cisco 兼容 IPSec) 服务端已就绪
------------------------------------------------------------------
 服务器地址   : $SERVER_IP
 端口/协议    : 500,4500/udp（IKEv1 主动模式 + XAUTH）
 组密钥(PSK)  : $VPN_PSK
 用户名/密码  : $USER_NAME / $USER_PASS
 VPN 网段     : $VPN_SUBNET $VPN_NETMASK（客户端地址池 $VPN_POOL）
 容器         : $CONTAINER（--net host --privileged）
 客户端配置   : $BUILD_DIR/clients/vpnc.conf
------------------------------------------------------------------
 客户端（vpnc）配置：
   IPSec gateway  $SERVER_IP
   IPSec ID       $USER_NAME
   IPSec secret   $VPN_PSK
   Xauth username $USER_NAME
   Xauth password $USER_PASS
------------------------------------------------------------------
 注意：500/4500 已被 VPNC 占用，strongSwan / L2TP/IPsec 需保持停用。
 管理：
   $SUDO docker stop  $CONTAINER
   $SUDO docker start $CONTAINER
   $SUDO docker logs -f $CONTAINER
   $SUDO docker exec -it $CONTAINER ipsec statusall
==================================================================
EOF
