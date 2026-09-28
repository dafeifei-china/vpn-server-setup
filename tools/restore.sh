#!/usr/bin/env bash
# 从备份归档恢复整个 VPN 工作区。
#
# 用法:
#   tools/restore.sh <vpn-workspace-YYYYmmdd-HHMMSS.tar.gz>        # 交互确认
#   tools/restore.sh <archive.tar.gz> --yes                        # 跳过确认
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BASE="$(basename "$ROOT")"

ARCHIVE="${1:-}"
[ -n "$ARCHIVE" ] || { echo "用法: $0 <archive.tar.gz> [--yes]" >&2; exit 1; }
[ -f "$ARCHIVE" ] || { echo "找不到归档: $ARCHIVE" >&2; exit 1; }
ARCHIVE="$(cd "$(dirname "$ARCHIVE")" && pwd)/$(basename "$ARCHIVE")"
CONFIRM="${2:-}"

if [ -f "$ARCHIVE.sha256" ]; then
  echo "[restore] 校验 SHA256..."
  ( cd "$(dirname "$ARCHIVE")" && sha256sum -c "$(basename "$ARCHIVE").sha256" )
fi

echo "[restore] 归档 : $ARCHIVE"
echo "[restore] 目标 : $ROOT"
if [ "$CONFIRM" != "--yes" ]; then
  read -r -p "将用归档内容覆盖目标目录，继续？[y/N] " ans
  case "$ans" in y|Y|yes|YES) ;; *) echo "已取消"; exit 1;; esac
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
tar xzf "$ARCHIVE" -C "$TMP"
SRC="$TMP/$BASE"
[ -d "$SRC" ] || { echo "归档结构不符（未找到 $BASE/）" >&2; exit 1; }

if command -v rsync >/dev/null 2>&1; then
  rsync -a "$SRC"/ "$ROOT"/
else
  cp -a "$SRC"/. "$ROOT"/
fi

echo "[restore] 完成。建议核对: git -C \"$ROOT\" status"
