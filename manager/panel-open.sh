#!/usr/bin/env bash
# 一键打开 VPN 统一管理面板：
#   1) 面板已在监听 -> 直接用默认浏览器打开；
#   2) 未在监听   -> 先尝试启动 systemd 服务（必要时弹图形授权），
#                    失败则用当前用户后台拉起 web.py；
#   3) 等端口就绪后打开浏览器。
# 可用环境变量覆盖：VPNCTL_HOST（默认 127.0.0.1）、VPNCTL_PORT（默认 8787）。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${VPNCTL_HOST:-127.0.0.1}"
PORT="${VPNCTL_PORT:-8787}"
URL="http://${HOST}:${PORT}/"
LOG="${HERE}/../state/panel-open.log"

is_up() { ss -H -ltn "sport = :${PORT}" 2>/dev/null | grep -q .; }

wait_up() {
  local i
  for i in $(seq 1 "$1"); do
    is_up && return 0
    sleep 0.3
  done
  return 1
}

mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

if ! is_up; then
  # 方案 A：交给已 enabled 的 systemd 服务（非 root 时用 pkexec 弹授权）
  if command -v systemctl >/dev/null 2>&1; then
    if [ "$(id -u)" -eq 0 ]; then
      systemctl start vpn-manager-web.service >/dev/null 2>&1 || true
    elif command -v pkexec >/dev/null 2>&1; then
      pkexec systemctl start vpn-manager-web.service >/dev/null 2>&1 || true
    else
      systemctl start vpn-manager-web.service >/dev/null 2>&1 || true
    fi
    wait_up 20 || true
  fi
fi

if ! is_up; then
  # 方案 B：当前用户后台直接拉起 web.py（无 sudo，随会话存续）
  ( cd "$HERE" && setsid nohup python3 web.py --host "$HOST" --port "$PORT" \
      >>"$LOG" 2>&1 & ) >/dev/null 2>&1
  wait_up 30 || true
fi

xdg-open "$URL" >/dev/null 2>&1 &
