#!/usr/bin/env bash
#
# setup-l2tp.sh — 在现有 Docker 上部署 L2TP/IPsec VPN（不影响已运行的 OpenVPN）
#
# 用法：
#   ./setup-l2tp.sh
#   SERVER_IP=192.168.0.106 ./setup-l2tp.sh
#
# 说明：
#   - 复用已安装的 Docker，不安装/改动 Docker
#   - 使用独立网络命名空间（桥接 + 端口映射），不修改宿主机 iptables
#   - 端口只用 500/4500 udp，与 OpenVPN 的 1194 不冲突
#   - VPN 网段 192.168.42.0/24，与 OpenVPN 的 10.99.99.0/24 不冲突
#   - 凭据存放在同目录 vpn.env（首次生成后复用）
#
set -euo pipefail

########################  配置区  ########################
IMAGE="${IMAGE:-hwdsl2/ipsec-vpn-server}"
CONTAINER="${CONTAINER:-ipsec-vpn-server}"
VOLUME="${VOLUME:-ikev2-vpn-data}"
DOCKER_REGISTRY="${DOCKER_REGISTRY:-docker.1panel.live}"   # 备用镜像源（无 https://）
OPENVPN_CONTAINER="${OPENVPN_CONTAINER:-running-vpn-openvpn-tls}"
SERVER_IP="${SERVER_IP:-}"
#########################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/vpn.env"

log()  { printf '\033[1;32m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
dk() { $SUDO docker "$@"; }

gen_secret() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c "$1"; }

# ------------------------------------------------------------------ #
# 0. 检查 Docker（不安装）
# ------------------------------------------------------------------ #
command -v docker >/dev/null 2>&1 || die "未找到 docker，请先安装（可参考 OpenVPN 的 setup-openvpn.sh）"
$SUDO docker info >/dev/null 2>&1 || die "Docker 未运行：systemctl start docker"

# 服务器地址
if [ -z "$SERVER_IP" ]; then
  SERVER_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
  [ -n "$SERVER_IP" ] || die "无法自动探测 IP，请用 SERVER_IP=x.x.x.x 运行"
  log "自动探测到 SERVER_IP=$SERVER_IP"
else
  log "使用指定 SERVER_IP=$SERVER_IP"
fi

# ------------------------------------------------------------------ #
# 1. 凭据（首次生成，之后复用；VPN_PUBLIC_IP 每次同步）
# ------------------------------------------------------------------ #
if [ ! -f "$ENV_FILE" ]; then
  log "生成 VPN 凭据 $ENV_FILE"
  {
    echo "VPN_IPSEC_PSK=$(gen_secret 24)"
    echo "VPN_USER=l2tpuser"
    echo "VPN_PASSWORD=$(gen_secret 16)"
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
else
  log "复用已有凭据 $ENV_FILE"
fi

# 始终把 VPN_PUBLIC_IP 同步为当前/传入的 IP：IP 变更后重跑本脚本即可生效
if [ -n "$SERVER_IP" ]; then
  if grep -q '^VPN_PUBLIC_IP=' "$ENV_FILE" 2>/dev/null; then
    _old_ip="$(grep '^VPN_PUBLIC_IP=' "$ENV_FILE" | cut -d= -f2-)"
    if [ "$_old_ip" != "$SERVER_IP" ]; then
      sed -i "s|^VPN_PUBLIC_IP=.*|VPN_PUBLIC_IP=$SERVER_IP|" "$ENV_FILE"
      log "已更新 VPN_PUBLIC_IP：$_old_ip -> $SERVER_IP"
    else
      log "VPN_PUBLIC_IP=$SERVER_IP（未变）"
    fi
  else
    echo "VPN_PUBLIC_IP=$SERVER_IP" >> "$ENV_FILE"
    log "已写入 VPN_PUBLIC_IP=$SERVER_IP"
  fi
fi

# ------------------------------------------------------------------ #
# 2. 拉取镜像（优先本地/加速器，失败再直连）
# ------------------------------------------------------------------ #
if ! dk image inspect "$IMAGE" >/dev/null 2>&1; then
  log "拉取镜像 $IMAGE ..."
  if ! dk pull "$IMAGE"; then
    warn "直连失败，改用镜像源 $DOCKER_REGISTRY"
    dk pull "$DOCKER_REGISTRY/hwdsl2/ipsec-vpn-server"
    dk tag "$DOCKER_REGISTRY/hwdsl2/ipsec-vpn-server" "$IMAGE"
  fi
else
  log "镜像已存在"
fi

# ------------------------------------------------------------------ #
# 3. 重建容器（桥接网络，与 OpenVPN 隔离）
# ------------------------------------------------------------------ #
if dk ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  log "移除旧容器 $CONTAINER"
  dk rm -f "$CONTAINER" >/dev/null
fi

log "启动 L2TP/IPsec 容器 $CONTAINER ..."
dk run -d --name "$CONTAINER" --restart=unless-stopped \
  --env-file "$ENV_FILE" \
  -v "$VOLUME:/etc/ipsec.d" \
  -v /lib/modules:/lib/modules:ro \
  -p 500:500/udp \
  -p 4500:4500/udp \
  --privileged \
  "$IMAGE" >/dev/null

log "等待服务就绪 ..."
sleep 10

# 兼容较新 Linux 客户端（UOS V20/V25 等）的默认 IKE 提议：
# 这些客户端的 strongSwan 默认第一个变换用 MODP3072/4096，而 hwdsl2 只配到
# MODP2048，会导致 IKE 第一阶段 NO_PROPOSAL_CHOSEN（客户端报“认证失败”）。
# 这里给服务端补上 MODP3072/4096，使新旧客户端都能用默认配置连上。
# 注意：不要加 MODP1024，Libreswan 5.4 会因整条 ike= 非法而连 l2tp-psk 都不加载。
IKE_ALGS_EXTRA="${IKE_ALGS_EXTRA:-aes256-sha2;modp3072,aes128-sha2;modp3072,aes256-sha1;modp3072,aes128-sha1;modp3072,aes256-sha2;modp4096,aes128-sha2;modp4096,aes256-sha2;dh19,aes128-sha2;dh19,aes256-sha2;dh20,aes128-sha2;dh20,aes256-sha2;dh21,aes128-sha2;dh21,aes256-sha2;dh31,aes128-sha2;dh31}"
if [ -n "$IKE_ALGS_EXTRA" ]; then
  log "补充 IKE 提议（MODP3072/4096）以兼容新客户端 ..."
  for _ in $(seq 1 30); do
    dk exec "$CONTAINER" grep -q '^  ike=' /etc/ipsec.conf 2>/dev/null && break
    sleep 1
  done
  dk exec "$CONTAINER" sed -i -E \
    "s|^(  ike=.*)\$|\\1,$IKE_ALGS_EXTRA|" /etc/ipsec.conf
  dk exec "$CONTAINER" ipsec restart >/dev/null 2>&1 || true
  sleep 2
fi

dk logs "$CONTAINER" | sed -n '/Connect to your new VPN/,/^$/p' || true

# ------------------------------------------------------------------ #
# 4. 校验：不影响 OpenVPN
# ------------------------------------------------------------------ #
echo "------------------------------------------------------------------"
if dk ps --format '{{.Names}}' | grep -qx "$OPENVPN_CONTAINER"; then
  log "OpenVPN 容器 $OPENVPN_CONTAINER 仍在运行 ✓"
else
  warn "OpenVPN 容器 $OPENVPN_CONTAINER 不在运行，请检查！"
fi
if ss -ulnp 2>/dev/null | grep -q ':1194 '; then
  log "OpenVPN 1194/udp 仍在监听 ✓"
else
  warn "未发现 1194/udp 监听"
fi
if ip -4 addr show tun0 >/dev/null 2>&1; then
  log "OpenVPN tun0 仍在 ✓"
fi

# ------------------------------------------------------------------ #
# 5. 输出连接信息
# ------------------------------------------------------------------ #
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a
cat <<EOF
==================================================================
 L2TP/IPsec VPN 已就绪
------------------------------------------------------------------
 服务器地址   : ${VPN_PUBLIC_IP:-$SERVER_IP}
 IPsec 预共享密钥(PSK): $VPN_IPSEC_PSK
 用户名       : $VPN_USER
 密码         : $VPN_PASSWORD
 VPN 网段     : 192.168.42.0/24
 容器         : $CONTAINER（端口 500/4500 udp）
------------------------------------------------------------------
 管理：
   $SUDO docker stop  $CONTAINER
   $SUDO docker start $CONTAINER
   $SUDO docker logs -f $CONTAINER
   $SUDO docker exec -it $CONTAINER ipsec status
==================================================================
EOF
