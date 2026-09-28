#!/usr/bin/env bash
# 备份整个 VPN 工作区（含机密/PKI/凭据/配置/SQLite 状态）。
# 归档含明文机密，权限置 600，请自行妥善保存。
#
# 用法:
#   tools/backup.sh                       # 备份到 $BACKUP_DIR（默认 ~/vpn-backups）
#   BACKUP_DIR=/mnt/usb tools/backup.sh   # 指定目录
#   KEEP=30 tools/backup.sh               # 保留最近 30 份（默认 14）
#
# 产出: <BACKUP_DIR>/vpn-workspace-YYYYmmdd-HHMMSS.tar.gz
#       (+ .sha256 校验 + .txt 清单)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$HOME/vpn-backups}"
KEEP="${KEEP:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
NAME="vpn-workspace-$STAMP"
ARCHIVE="$BACKUP_DIR/$NAME.tar.gz"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" 2>/dev/null || true

echo "[backup] 源目录 : $ROOT"
echo "[backup] 目标   : $ARCHIVE"

SERVER_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')" || SERVER_IP="?"
if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  GIT_REV="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo '-')"
  GIT_DIRTY="$(git -C "$ROOT" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
else
  GIT_REV="-"; GIT_DIRTY="0"
fi

# 打包整棵工作区（含 .git 与机密）；排除缓存与旧归档自身
tar czf "$ARCHIVE" \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='vpn-workspace-*.tar.gz' \
  -C "$(dirname "$ROOT")" "$(basename "$ROOT")"

chmod 600 "$ARCHIVE"
( cd "$BACKUP_DIR" && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256" )

cat > "$ARCHIVE.txt" <<EOF
备份时间 : $(date '+%F %T %z')
主机     : $(hostname)
服务器IP : $SERVER_IP
git 版本 : $GIT_REV (未提交变更 $GIT_DIRTY 项)
归档     : $NAME.tar.gz ($(du -h "$ARCHIVE" | cut -f1))
SHA256   : $(cut -d' ' -f1 "$ARCHIVE.sha256")
机密     : 归档内含全部 PKI/私钥/凭据/令牌，妥善保管
EOF

# 轮转：仅保留最近 KEEP 份
ls -1t "$BACKUP_DIR"/vpn-workspace-*.tar.gz 2>/dev/null \
  | tail -n "+$((KEEP + 1))" \
  | while read -r old; do
      rm -f "$old" "$old.sha256" "${old%.tar.gz}.txt"
      echo "[backup] 删除旧归档 $(basename "$old")"
    done

echo "[backup] 完成 -> $ARCHIVE"
echo "[backup] 清单 -> $ARCHIVE.txt"
