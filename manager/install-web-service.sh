#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
PY="$(command -v python3)"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8787}"
UNIT="/etc/systemd/system/vpn-manager-web.service"

echo "安装 vpnctl 网页面板为系统服务"
echo "  用户: $RUN_USER    Python: $PY"
echo "  目录: $HERE        监听: $HOST:$PORT"

sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=vpnctl 网页面板 (VPN 统一管理)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$HERE
ExecStart=$PY -u $HERE/web.py --host $HOST --port $PORT
Restart=on-failure
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable vpn-manager-web.service
sudo systemctl restart vpn-manager-web.service
sleep 1
sudo systemctl --no-pager --full status vpn-manager-web.service | head -12 || true
echo
echo "令牌（页面右上角填写）:"
sudo journalctl -u vpn-manager-web.service --no-pager | grep -o '操作令牌: .*' | tail -1 || true
echo
echo "查看日志: sudo journalctl -u vpn-manager-web.service -f"
echo "停止服务: sudo systemctl disable --now vpn-manager-web.service"
