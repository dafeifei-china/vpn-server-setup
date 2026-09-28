#!/usr/bin/env bash
#
# setup-openvpn.sh — 一键搭建 Docker 版 OpenVPN 服务端（自动生成证书与客户端配置）
#
# 用法：
#   ./setup-openvpn.sh                              # 自动探测本机 IP
#   SERVER_IP=192.168.0.106 ./setup-openvpn.sh      # 指定客户端连接地址
#   FULL_TUNNEL=yes ./setup-openvpn.sh              # 全流量走 VPN
#   CLIENT_PASS= ./setup-openvpn.sh                 # 不生成加密私钥
#
# 特点：
#   - 自动选择 Docker 安装方式：优先 apt，失败则用清华静态二进制（UOS 等源被劫持时）
#   - 自动生成 CA/服务端/客户端证书，有效期 10 年
#   - 自动生成可直接使用的客户端配置（自包含 .ovpn/.conf + 拆分证书）
#   - 可重复运行：已有证书会复用，镜像重建、容器重建
#
set -euo pipefail

########################  配置区（可按需修改）  ########################
SERVER_IP="${SERVER_IP:-}"                 # 客户端 remote 地址；留空自动探测
PORT="${PORT:-1194}"
PROTO="${PROTO:-udp}"
VPN_SUBNET="${VPN_SUBNET:-10.99.99.0}"     # 隧道网段
VPN_NETMASK="${VPN_NETMASK:-255.255.255.0}"
CA_CN="${CA_CN:-local-vpn-ca}"
CLIENT_NAME="${CLIENT_NAME:-client}"
CLIENT_PASS="${CLIENT_PASS:-test}"         # 加密私钥口令；置空则不生成加密版
FULL_TUNNEL="${FULL_TUNNEL:-no}"           # yes = 全流量走 VPN
DNS1="${DNS1:-8.8.8.8}"

IMAGE="${IMAGE:-vpn-openvpn-tls}"
CONTAINER="${CONTAINER:-running-vpn-openvpn-tls}"
BASE_IMAGE="${BASE_IMAGE:-ubuntu:20.04}"
DOCKER_VERSION="${DOCKER_VERSION:-29.8.1}"
DOCKER_MIRROR="${DOCKER_MIRROR:-https://docker.m.daocloud.io}"
TUNA="${TUNA:-https://mirrors.tuna.tsinghua.edu.cn}"
EASYRSA_VER="${EASYRSA_VER:-3.1.7}"
########################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/local-server"
EASYRSA_DIR="$BUILD_DIR/etc/openvpn/easy-rsa"
PKI="$EASYRSA_DIR/pki"
CLIENTS_DIR="$BUILD_DIR/clients"

log()  { printf '\033[1;32m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
dk() { $SUDO docker "$@"; }

# ------------------------------------------------------------------ #
# 0. sudo / IP 探测
# ------------------------------------------------------------------ #
if [ -n "$SUDO" ]; then
  log "本操作需要 sudo 权限（安装 Docker、启动容器）"
  $SUDO -v || die "无法获取 sudo 权限"
fi

if [ -z "$SERVER_IP" ]; then
  SERVER_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
  [ -n "$SERVER_IP" ] || die "无法自动探测 IP，请用 SERVER_IP=x.x.x.x 运行本脚本"
  log "自动探测到 SERVER_IP=$SERVER_IP"
else
  log "使用指定 SERVER_IP=$SERVER_IP"
fi

# ------------------------------------------------------------------ #
# 1. Docker
# ------------------------------------------------------------------ #
install_static_docker() {
  local arch url
  arch="$(uname -m)"
  case "$arch" in x86_64|aarch64) :;; *) die "不支持的架构：$arch";; esac
  url="$TUNA/docker-ce/linux/static/stable/$arch/docker-$DOCKER_VERSION.tgz"
  log "下载 Docker 静态二进制：$url"
  curl -fL -o /tmp/docker-static.tgz "$url" || die "下载 Docker 失败"
  rm -rf /tmp/docker-static && mkdir -p /tmp/docker-static
  tar xzf /tmp/docker-static.tgz -C /tmp/docker-static
  $SUDO install -m0755 /tmp/docker-static/docker/* /usr/local/bin/

  if [ ! -f /etc/systemd/system/docker.service ]; then
    $SUDO tee /etc/systemd/system/docker.service >/dev/null <<'EOF'
[Unit]
Description=Docker Application Container Engine
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/local/bin/dockerd -H unix:///var/run/docker.sock
ExecReload=/bin/kill -s HUP $MAINPID
Restart=always
RestartSec=2
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
  fi
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now docker >/dev/null 2>&1 || true
}

ensure_docker() {
  if command -v docker >/dev/null 2>&1 && $SUDO docker info >/dev/null 2>&1; then
    log "Docker 已就绪"
    return
  fi

  if command -v apt-get >/dev/null 2>&1; then
    log "尝试 apt 安装 docker.io ..."
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get update \
      -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15 >/dev/null 2>&1 || true
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io >/dev/null 2>&1 || true
    $SUDO systemctl enable --now docker >/dev/null 2>&1 || true
  fi

  if ! command -v docker >/dev/null 2>&1 || ! $SUDO docker info >/dev/null 2>&1; then
    warn "apt 方式不可用（常见于 UOS 源被内网 VPN 劫持），改用静态二进制"
    install_static_docker
  fi

  if [ ! -f /etc/docker/daemon.json ]; then
    log "写入镜像加速配置 /etc/docker/daemon.json"
    printf '{\n  "registry-mirrors": ["%s"]\n}\n' "$DOCKER_MIRROR" | $SUDO tee /etc/docker/daemon.json >/dev/null
    $SUDO systemctl restart docker >/dev/null 2>&1 || true
  fi

  getent group docker >/dev/null 2>&1 || $SUDO groupadd -r docker
  $SUDO usermod -aG docker "${SUDO_USER:-$(id -un)}" >/dev/null 2>&1 || true
  $SUDO systemctl restart docker >/dev/null 2>&1 || true

  $SUDO docker info >/dev/null 2>&1 || die "Docker 启动失败，请检查 systemctl status docker"
  log "Docker 就绪"
}

pull_base() {
  dk image inspect "$BASE_IMAGE" >/dev/null 2>&1 && return
  log "拉取基础镜像 $BASE_IMAGE ..."
  if ! dk pull "$BASE_IMAGE"; then
    warn "直连失败，改用镜像源 $DOCKER_MIRROR"
    dk pull "$DOCKER_MIRROR/library/ubuntu:20.04"
    dk tag "$DOCKER_MIRROR/library/ubuntu:20.04" "$BASE_IMAGE"
  fi
}

# ------------------------------------------------------------------ #
# 2. EasyRSA
# ------------------------------------------------------------------ #
ensure_easyrsa() {
  [ -x "$EASYRSA_DIR/easyrsa" ] && return
  mkdir -p "$BUILD_DIR/etc/openvpn"
  if [ -d /usr/share/easy-rsa ]; then
    log "使用系统 easy-rsa (/usr/share/easy-rsa)"
    cp -a /usr/share/easy-rsa "$EASYRSA_DIR"
    return
  fi
  log "下载 EasyRSA $EASYRSA_VER ..."
  curl -fL -o /tmp/EasyRSA.tgz \
    "https://github.com/OpenVPN/easy-rsa/releases/download/v$EASYRSA_VER/EasyRSA-$EASYRSA_VER.tgz" \
    || die "下载 EasyRSA 失败（GitHub 不可达）"
  tar xzf /tmp/EasyRSA.tgz -C "$BUILD_DIR/etc/openvpn"
  mv "$BUILD_DIR/etc/openvpn/EasyRSA-$EASYRSA_VER" "$EASYRSA_DIR"
}

# ------------------------------------------------------------------ #
# 3. 生成服务端文件
# ------------------------------------------------------------------ #
write_server_files() {
  mkdir -p "$BUILD_DIR/etc/openvpn" "$CLIENTS_DIR"

  cat > "$BUILD_DIR/Dockerfile" <<EOF
FROM $BASE_IMAGE

ENV DEBIAN_FRONTEND noninteractive
RUN sed -i 's|//archive.ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g; \\
           s|//security.ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list \\
 && apt-get update \\
 && apt-get install -y iptables openvpn iproute2 \\
 && rm -rf /var/lib/apt/lists/*

COPY ./etc/openvpn/openvpn.conf /etc/openvpn/openvpn.conf
COPY ./etc/openvpn/easy-rsa/ /etc/openvpn/easy-rsa/
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0700 /entrypoint.sh

EXPOSE $PORT/udp
ENTRYPOINT ["/entrypoint.sh"]
CMD openvpn --config /etc/openvpn/openvpn.conf
EOF

  cat > "$BUILD_DIR/entrypoint.sh" <<EOF
#!/bin/sh
set -e
sysctl -w net.ipv4.ip_forward=1
iptables -t nat -C POSTROUTING -s $VPN_SUBNET/24 ! -d $VPN_SUBNET/24 -j MASQUERADE 2>/dev/null \\
  || iptables -t nat -A POSTROUTING -s $VPN_SUBNET/24 ! -d $VPN_SUBNET/24 -j MASQUERADE
iptables -C FORWARD -s $VPN_SUBNET/24 -p tcp \\
  -m tcp --tcp-flags FIN,SYN,RST,ACK SYN -j TCPMSS --set-mss 1356 2>/dev/null \\
  || iptables -A FORWARD -s $VPN_SUBNET/24 -p tcp \\
  -m tcp --tcp-flags FIN,SYN,RST,ACK SYN -j TCPMSS --set-mss 1356
exec "\$@"
EOF
  chmod +x "$BUILD_DIR/entrypoint.sh"

  cat > "$BUILD_DIR/etc/openvpn/openvpn.conf" <<EOF
tls-server
port $PORT
proto $PROTO
dev tun
ca   /etc/openvpn/easy-rsa/pki/ca.crt
cert /etc/openvpn/easy-rsa/pki/issued/server.crt
key  /etc/openvpn/easy-rsa/pki/private/server.key
dh   /etc/openvpn/easy-rsa/pki/dh.pem
server $VPN_SUBNET $VPN_NETMASK
client-to-client
keepalive 10 120
persist-key
persist-tun
cipher AES-256-CBC
ncp-ciphers AES-256-GCM:AES-128-GCM
verb 3
EOF

  if [ "$FULL_TUNNEL" = "yes" ]; then
    cat >> "$BUILD_DIR/etc/openvpn/openvpn.conf" <<EOF
push "redirect-gateway def1 bypass-dhcp"
push "dhcp-option DNS $DNS1"
EOF
  fi
}

# ------------------------------------------------------------------ #
# 4. 生成证书
# ------------------------------------------------------------------ #
gen_certs() {
  if [ -f "$PKI/ca.crt" ]; then
    log "证书已存在，复用 $PKI（如需重签请先删除该目录）"
    return
  fi
  log "生成证书（CA / server / $CLIENT_NAME，有效期 10 年）..."
  (
    cd "$EASYRSA_DIR"
    export EASYRSA_BATCH=1 EASYRSA_ALGO=rsa EASYRSA_KEY_SIZE=2048 \
           EASYRSA_CA_EXPIRE=3650 EASYRSA_CERT_EXPIRE=3650
    ./easyrsa init-pki >/dev/null
    EASYRSA_REQ_CN="$CA_CN" ./easyrsa build-ca nopass >/dev/null
    ./easyrsa build-server-full server nopass >/dev/null
    ./easyrsa build-client-full "$CLIENT_NAME" nopass >/dev/null
    ./easyrsa gen-dh >/dev/null
  )
}

# ------------------------------------------------------------------ #
# 5. 生成客户端配置
# ------------------------------------------------------------------ #
gen_clients() {
  log "生成客户端配置到 $CLIENTS_DIR"
  local key_plain="$PKI/private/$CLIENT_NAME.key"
  local key_enc="$CLIENTS_DIR/$CLIENT_NAME.key.enc"
  local crt="$PKI/issued/$CLIENT_NAME.crt"

  cp "$PKI/ca.crt" "$CLIENTS_DIR/ca.crt"
  cp "$crt"        "$CLIENTS_DIR/$CLIENT_NAME.crt"
  cp "$key_plain"  "$CLIENTS_DIR/$CLIENT_NAME.key"
  chmod 600 "$CLIENTS_DIR/$CLIENT_NAME.key"

  build_ovpn() {   # $1=私钥文件  $2=输出
    {
      printf '%s\n' 'client' 'dev tun' "proto $PROTO" "remote $SERVER_IP $PORT" \
        'resolv-retry infinite' 'nobind' 'persist-key' 'persist-tun' \
        'remote-cert-tls server' 'auth-nocache' 'verb 3'
      printf '\n<ca>\n';   cat "$PKI/ca.crt"
      printf '</ca>\n<cert>\n'; cat "$crt"
      printf '</cert>\n<key>\n'; cat "$1"
      printf '</key>\n'
    } > "$2"
    chmod 600 "$2"
  }

  build_ovpn "$key_plain" "$CLIENTS_DIR/$CLIENT_NAME-nopass.ovpn"
  cp "$CLIENTS_DIR/$CLIENT_NAME-nopass.ovpn" "$CLIENTS_DIR/$CLIENT_NAME-nopass.conf"

  if [ -n "$CLIENT_PASS" ]; then
    openssl pkcs8 -topk8 -in "$key_plain" -out "$key_enc" \
      -passout "pass:$CLIENT_PASS" -v2 aes-256-cbc
    chmod 600 "$key_enc"
    build_ovpn "$key_enc" "$CLIENTS_DIR/$CLIENT_NAME.ovpn"
    cp "$CLIENTS_DIR/$CLIENT_NAME.ovpn" "$CLIENTS_DIR/$CLIENT_NAME.conf"
  else
    cp "$CLIENTS_DIR/$CLIENT_NAME-nopass.ovpn" "$CLIENTS_DIR/$CLIENT_NAME.ovpn"
    cp "$CLIENTS_DIR/$CLIENT_NAME-nopass.ovpn" "$CLIENTS_DIR/$CLIENT_NAME.conf"
  fi
}

# ------------------------------------------------------------------ #
# 6. 构建镜像 + 启动容器
# ------------------------------------------------------------------ #
build_and_run() {
  log "构建镜像 $IMAGE ..."
  dk build -t "$IMAGE" "$BUILD_DIR"

  if dk ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    log "移除旧容器 $CONTAINER"
    dk rm -f "$CONTAINER" >/dev/null
  fi

  log "启动容器 $CONTAINER ..."
  dk run -d --net host --privileged --restart unless-stopped \
    --name "$CONTAINER" "$IMAGE" >/dev/null

  sleep 5
  if dk logs --tail 80 "$CONTAINER" 2>&1 | grep -q "Initialization Sequence Completed"; then
    log "OpenVPN 服务端启动成功"
  else
    warn "未检测到启动成功日志，请执行：$SUDO docker logs $CONTAINER"
  fi
}

# ------------------------------------------------------------------ #
main() {
  ensure_docker
  ensure_easyrsa
  write_server_files
  gen_certs
  gen_clients
  pull_base
  build_and_run

  local ip; ip="$(ip -4 addr show 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | grep -v '^127\.' | head -1)"
  cat <<EOF

==================================================================
 OpenVPN 服务端已就绪
------------------------------------------------------------------
 服务端地址   : $SERVER_IP
 端口/协议    : $PORT/$PROTO
 隧道网段     : $VPN_SUBNET $VPN_NETMASK（服务端 .1）
 镜像 / 容器  : $IMAGE / $CONTAINER
 客户端文件   : $CLIENTS_DIR

 客户端配置（已内嵌证书，可直接分发）：
   $CLIENTS_DIR/$CLIENT_NAME.ovpn        （加密私钥，口令：${CLIENT_PASS:-无}）
   $CLIENTS_DIR/$CLIENT_NAME-nopass.ovpn（未加密私钥）
   $CLIENTS_DIR/$CLIENT_NAME.conf        （供 UOS 图形界面导入）
   拆分文件: ca.crt  $CLIENT_NAME.crt  $CLIENT_NAME.key[.enc]

 客户端连接：
   sudo openvpn --config $CLIENT_NAME.ovpn
   或图形界面导入 $CLIENT_NAME.conf

 管理：
   $SUDO docker stop  $CONTAINER      # 停用
   $SUDO docker start $CONTAINER      # 启用
   $SUDO docker logs -f $CONTAINER    # 日志
==================================================================
EOF

  if [ "$FULL_TUNNEL" != "yes" ]; then
    echo "提示：当前为分流模式（仅 $VPN_SUBNET/24 走 VPN）。要全流量走 VPN，用 FULL_TUNNEL=yes 重跑。"
  fi
  echo "提示：云服务器/跨网访问需在安全组放行 $PORT/$PROTO。"
}

main "$@"
