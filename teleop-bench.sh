#!/usr/bin/env bash
# 遥操测试台桌面版启动脚本（应用菜单里的图标调用它）
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$HERE/backend"
PY="${BENCH_PYTHON:-}"
for cand in "$HOME/miniforge3/envs/teleop-bench/bin/python" "$HOME/.local/share/teleop-bench/venv/bin/python" "$HOME/miniforge3/envs/webdev/bin/python"; do
	[ -z "$PY" ] && [ -x "$cand" ] && PY="$cand"
done
exec "${PY:?没找到 Python 环境，先运行 install.sh}" desktop.py "$@"
