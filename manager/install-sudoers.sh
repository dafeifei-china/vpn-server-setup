#!/usr/bin/env bash
# 一次性授权：让管理面板（非 root 运行）能启停/禁用宿主机 VPN 服务（目前仅 xl2tpd）。
# 只放开 systemctl 针对 xl2tpd 的几个子命令，其它命令仍需密码。
# 用法： sudo ./install-sudoers.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root/sudo 运行：sudo $0" >&2
  exit 1
fi

RUN_USER="${SUDO_USER:-$(id -un)}"
if [ "$RUN_USER" = "root" ]; then
  echo "当前已是 root，运行面板无需此授权。" >&2
  exit 0
fi

SYSCTL="$(command -v systemctl)"
JOURNALCTL="$(command -v journalctl)"
RULE="/etc/sudoers.d/vpn-manager"

TMP="$(mktemp)"
cat >"$TMP" <<EOF
# VPN 管控平台：允许 $RUN_USER 无密码管理/读取宿主机 xl2tpd（由 install-sudoers.sh 生成）
$RUN_USER ALL=(root) NOPASSWD: $SYSCTL start xl2tpd*, $SYSCTL stop xl2tpd*, \\
    $SYSCTL restart xl2tpd*, $SYSCTL enable xl2tpd*, $SYSCTL disable xl2tpd*, \\
    $SYSCTL is-active xl2tpd*, $SYSCTL status xl2tpd*, $JOURNALCTL -u xl2tpd*
EOF

visudo -cf "$TMP"
install -m 0440 -o root -g root "$TMP" "$RULE"
rm -f "$TMP"

echo "已安装 $RULE ：$RUN_USER 可无密码管理 xl2tpd"
echo "验证： sudo -n -u root $SYSCTL is-active xl2tpd"
echo "然后重启面板（如 systemctl --user restart 或重新运行 web.py）以生效。"
