#!/usr/bin/env bash
# 用 Chrome 全屏（kiosk）打开 flash.html：bash flash_fullscreen.sh [显示器=1]
#   显示器编号见 xrandr --listmonitors；本机 0 = 笔记本屏 1440x900，1 = 外接 2560x1440。
#   退出：Alt+F4。用独立的用户数据目录，不影响平时用的 Chrome。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MON="${1:-1}"
POS=$(xrandr --listmonitors | awk -v m="$MON:" '$1==m {split($3,a,"+"); print a[2]","a[3]}')
[ -n "$POS" ] || { echo "没有显示器 $MON"; xrandr --listmonitors; exit 1; }
exec google-chrome --user-data-dir="$HOME/.cache/flash-chrome" --no-first-run --kiosk \
	--ozone-platform=x11 --window-position="$POS" "file://$HERE/flash.html" >/dev/null 2>&1
