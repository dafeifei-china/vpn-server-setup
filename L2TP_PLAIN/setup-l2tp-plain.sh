#!/usr/bin/env bash
#
# setup-l2tp-plain.sh — 部署裸 L2TP（无 IPsec）服务端
#
# ⚠️ 裸 L2TP 没有加密，账号密码和流量明文传输，仅限可信网络/测试使用。
#
# 用法：
#   ./setup-l2tp-plain.sh
#   SERVER_IP=192.168.0.106 ./setup-l2tp-plain.sh
#
# 说明：
#   - 使用宿主机 xl2tpd + pppd，监听 UDP 1701
#   - 网段默认 192.168.44.0/24，与 L2TP/IPsec（192.168.42.0/24）、OpenVPN（10.99.99.0/24）均不冲突
#   - 不改动 OpenVPN / L2TP-IPSEC 容器
#   - NAT/转发规则写入 l2tp-plain-nat.service，服务器重启后自动恢复
#   - 可重复运行：覆盖配置前先备份
#
set -euo pipefail

########################  配置区  ########################
PORT="${PORT:-1701}"
VPN_SUBNET="${VPN_SUBNET:-192.168.44.0}"
VPN_NETMASK="${VPN_NETMASK:-255.255.255.0}"
LOCAL_IP="${LOCAL_IP:-192.168.44.1}"
POOL_START="${POOL_START:-192.168.44.10}"
POOL_END="${POOL_END:-192.168.44.250}"
SERVER_NAME="${SERVER_NAME:-l2tpserver}"
USER_NAME="${USER_NAME:-test}"
USER_PASS="${USER_PASS:-test}"
DNS1="${DNS1:-223.5.5.5}"
DNS2="${DNS2:-119.29.29.29}"
SERVER_IP="${SERVER_IP:-}"

XL2TPD_CONF=/etc/xl2tpd/xl2tpd.conf
PPP_OPT=/etc/ppp/options.xl2tpd
CHAP=/etc/ppp/chap-secrets
NAT_SCRIPT=/usr/local/sbin/l2tp-plain-nat.sh
NAT_UNIT=/etc/systemd/system/l2tp-plain-nat.service
#########################################################

log()  { printf '\033[1;32m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] && SUDO="" || SUDO="sudo"

if [ -z "$SERVER_IP" ]; then
  SERVER_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
  [ -n "$SERVER_IP" ] || die "无法自动探测 IP，请用 SERVER_IP=x.x.x.x 运行"
  log "自动探测到 SERVER_IP=$SERVER_IP"
else
  log "使用指定 SERVER_IP=$SERVER_IP"
fi

# 1. 依赖
command -v xl2tpd >/dev/null 2>&1 || { log "安装 xl2tpd ppp ..."; $SUDO apt-get install -y xl2tpd ppp; }
command -v pppd   >/dev/null 2>&1 || { log "安装 ppp ...";     $SUDO apt-get install -y ppp; }

# 2. 备份并写配置
$SUDO mkdir -p /etc/xl2tpd
if [ -f "$XL2TPD_CONF" ] && [ ! -f "$XL2TPD_CONF.bak" ]; then
  $SUDO cp -a "$XL2TPD_CONF" "$XL2TPD_CONF.bak"
  log "已备份原配置到 $XL2TPD_CONF.bak"
fi

$SUDO tee "$XL2TPD_CONF" >/dev/null <<EOF
[global]
port = $PORT
access control = no

[lns default]
ip range = $POOL_START-$POOL_END
local ip = $LOCAL_IP
require chap = yes
refuse pap = yes
require authentication = yes
name = $SERVER_NAME
ppp debug = no
pppoptfile = $PPP_OPT
length bit = yes
EOF
log "写入 $XL2TPD_CONF"

$SUDO tee "$PPP_OPT" >/dev/null <<EOF
require-mschap-v2
ms-dns $DNS1
ms-dns $DNS2
auth
mtu 1400
mru 1400
hide-password
name $SERVER_NAME
EOF
log "写入 $PPP_OPT"

# 3. 账号（不破坏已有条目）
if [ ! -f "$CHAP" ]; then
  echo "# client  server  secret  IP addresses" | $SUDO tee "$CHAP" >/dev/null
  $SUDO chmod 600 "$CHAP"
fi
if ! $SUDO grep -qE "^${USER_NAME}[[:space:]]+${SERVER_NAME}[[:space:]]" "$CHAP"; then
  echo "$USER_NAME $SERVER_NAME $USER_PASS *" | $SUDO tee -a "$CHAP" >/dev/null
  log "已添加账号 $USER_NAME"
else
  log "账号 $USER_NAME 已存在"
fi

# 4. 转发 + NAT + MSS（并持久化，重启后自动恢复）
echo 'net.ipv4.ip_forward=1' | $SUDO tee /etc/sysctl.d/99-l2tp-plain.conf >/dev/null
$SUDO sysctl -q -w net.ipv4.ip_forward=1

add_rule() {
  local table="$1"; shift
  $SUDO iptables -w 5 -t "$table" -C "$@" 2>/dev/null \
    || $SUDO iptables -w 5 -t "$table" -A "$@"
}
add_rule nat POSTROUTING -s "$VPN_SUBNET/24" ! -d "$VPN_SUBNET/24" -j MASQUERADE
add_rule filter FORWARD -s "$VPN_SUBNET/24" -j ACCEPT
add_rule filter FORWARD -d "$VPN_SUBNET/24" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
add_rule filter FORWARD -s "$VPN_SUBNET/24" -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
log "已配置转发/NAT"

# iptables 规则不会跨重启保留：写入恢复脚本，并交给 systemd 开机执行
log "写入开机自动恢复规则 $NAT_SCRIPT"
$SUDO tee "$NAT_SCRIPT" >/dev/null <<NAT_SCRIPT_EOF
#!/usr/bin/env bash
# 由 setup-l2tp-plain.sh 生成：开机恢复裸 L2TP 的转发/NAT 规则。
set -euo pipefail
VPN_CIDR="$VPN_SUBNET/24"
IPT="\$(command -v iptables || echo /usr/sbin/iptables)"
sysctl -qw net.ipv4.ip_forward=1 || true
add_rule() {
  local table="\$1"; shift
  "\$IPT" -w 5 -t "\$table" -C "\$@" 2>/dev/null || "\$IPT" -w 5 -t "\$table" -A "\$@"
}
add_rule nat POSTROUTING -s "\$VPN_CIDR" ! -d "\$VPN_CIDR" -j MASQUERADE
add_rule filter FORWARD -s "\$VPN_CIDR" -j ACCEPT
add_rule filter FORWARD -d "\$VPN_CIDR" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
add_rule filter FORWARD -s "\$VPN_CIDR" -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
NAT_SCRIPT_EOF
$SUDO chmod 0755 "$NAT_SCRIPT"

$SUDO tee "$NAT_UNIT" >/dev/null <<NAT_UNIT_EOF
[Unit]
Description=L2TP plain (no IPsec) NAT/forwarding rules
After=network-online.target
Wants=network-online.target
Before=xl2tpd.service

[Service]
Type=oneshot
ExecStart=$NAT_SCRIPT
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
NAT_UNIT_EOF
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now l2tp-plain-nat.service >/dev/null 2>&1 || true
log "已启用 l2tp-plain-nat.service（重启后自动恢复 NAT）"

# 5. 启动
$SUDO systemctl enable xl2tpd >/dev/null 2>&1 || true
$SUDO systemctl restart xl2tpd
sleep 2
systemctl is-active --quiet xl2tpd || die "xl2tpd 启动失败，检查 journalctl -u xl2tpd"
log "xl2tpd 已启动"

# 6. 校验
echo "------------------------------------------------------------------"
if ss -ulnp 2>/dev/null | grep -q ":$PORT "; then
  log "监听 $PORT/udp ✓"
else
  warn "未发现 $PORT/udp 监听"
fi
if command -v docker >/dev/null 2>&1 && $SUDO docker ps --format '{{.Names}}' 2>/dev/null | grep -qx running-vpn-openvpn-tls; then
  log "OpenVPN 容器仍在运行 ✓"
fi
if command -v docker >/dev/null 2>&1 && $SUDO docker ps --format '{{.Names}}' 2>/dev/null | grep -qx ipsec-vpn-server; then
  log "L2TP/IPsec 容器仍在运行 ✓"
fi

# 7. 输出
cat <<EOF
==================================================================
 裸 L2TP（无加密）已就绪
------------------------------------------------------------------
 服务器地址   : $SERVER_IP
 端口         : $PORT/udp
 用户名/密码  : $USER_NAME / $USER_PASS
 VPN 网段     : $VPN_SUBNET $VPN_NETMASK（服务端 $LOCAL_IP）
 配置文件     : $XL2TPD_CONF  /  $PPP_OPT  /  $CHAP
------------------------------------------------------------------
 ⚠️ 裸 L2TP 无加密，请仅在可信网络使用。
 客户端：L2TP 类型，且**不要启用 IPsec**。
------------------------------------------------------------------
 管理：
   $SUDO systemctl status xl2tpd
   $SUDO systemctl restart xl2tpd
   $SUDO systemctl stop xl2tpd
   $SUDO journalctl -u xl2tpd -f
   $SUDO systemctl status l2tp-plain-nat
   $SUDO iptables -t nat -S POSTROUTING | grep 192.168.44
==================================================================
EOF
