#!/usr/bin/env bash
# 启动遥操测试台：后端 + 已构建的前端，浏览器打开 http://127.0.0.1:8765
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${BENCH_PYTHON:-}"
for cand in "$HOME/miniforge3/envs/teleop-bench/bin/python" "$HOME/.local/share/teleop-bench/venv/bin/python" "$HOME/miniforge3/envs/webdev/bin/python"; do
	[ -z "$PY" ] && [ -x "$cand" ] && PY="$cand"
done
: "${PY:?没找到 Python 环境，先运行 install.sh}"
if [ ! -f "$HERE/frontend/dist/index.html" ]; then
	echo "界面还没构建：REBUILD_UI=1 bash install.sh"
	exit 1
fi
cd "$HERE/backend"
exec "$PY" -m uvicorn bench.app:app --host 127.0.0.1 --port "${PORT:-8765}"
