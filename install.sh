#!/usr/bin/env bash
# 遥操测试台安装脚本（Ubuntu 22.04 / 24.04）。用法：bash install.sh
# 做的事：建 Python 环境 → 装后端依赖 → 界面（包里已构建好，缺了才现场构建）→ 检查 Qt 依赖 → 加应用菜单和桌面图标 → 自检。
# Python 环境：装了 Miniforge（mamba）就建 conda 环境 teleop-bench（Python 3.11）；
#              没装就用系统 python3 建虚拟环境 ~/.local/share/teleop-bench/venv（24.04 自带 3.12）。
# 第一次连某台机器人前，还要运行一次 bash robot-access.sh（授权本机登录这台机器人）。
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ENV_NAME="${ENV_NAME:-teleop-bench}"
VENV="${VENV:-$HOME/.local/share/teleop-bench/venv}"

# 图形界面或部分工具解压 zip 会丢可执行位
chmod +x "$HERE"/*.sh "$HERE"/backend/tools/*.sh "$HERE"/backend/tests/fake/ffmpeg "$HERE"/backend/tests/fake/lark-cli 2>/dev/null || true

echo "== 1/5 建 Python 环境"
MAMBA="${MAMBA:-$(command -v mamba || echo "$HOME/miniforge3/bin/mamba")}"
if [ -z "${USE_VENV:-}" ] && [ -x "$MAMBA" ]; then
	ROOT="$(cd "$(dirname "$(readlink -f "$MAMBA")")/.." && pwd)"  # miniforge3/bin/mamba → miniforge3
	ENV_DIR="$ROOT/envs/$ENV_NAME"
	[ -x "$ENV_DIR/bin/python" ] || "$MAMBA" create -y -q -n "$ENV_NAME" -c conda-forge python=3.11
	PY="$ENV_DIR/bin/python"
	echo "   用 conda 环境 $ENV_DIR"
else
	SYS_PY="${SYS_PY:-$(command -v python3 || true)}"
	[ -n "$SYS_PY" ] || { echo "没找到 python3：sudo apt install python3 python3-venv"; exit 1; }
	"$SYS_PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "需要 Python 3.10 以上，当前 $("$SYS_PY" -V)"; exit 1; }
	if [ ! -x "$VENV/bin/python" ]; then
		mkdir -p "$(dirname "$VENV")"
		"$SYS_PY" -m venv "$VENV" 2>/dev/null || {
			echo "建虚拟环境失败：先运行 sudo apt install python3-venv（24.04 是 python3.12-venv），再重新运行 install.sh"; exit 1; }
	fi
	PY="$VENV/bin/python"
	echo "   用虚拟环境 $VENV（$("$PY" -V)）"
fi

echo "== 2/5 装后端依赖（第一次要下载约 300 MB）"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r "$HERE/backend/requirements.txt"

echo "== 3/5 界面"
if [ -f "$HERE/frontend/dist/index.html" ] && [ -z "${REBUILD_UI:-}" ]; then
	echo "   用包里已构建好的界面"
else
	command -v npm >/dev/null || { echo "要重新构建界面需要 Node 20：sudo apt install nodejs npm，或装 Miniforge 后 mamba install nodejs=20"; exit 1; }
	cd "$HERE/frontend"
	npm install --no-audit --no-fund >/dev/null
	"$PY" "$HERE/scripts/repair_node_modules.py"
	npx tsc -b
	npx vite build >/dev/null
	[ -f dist/index.html ] || { echo "界面构建失败"; exit 1; }
fi

echo "== 4/5 检查 Qt 窗口依赖"
QT_ERR="$(cd "$HERE/backend" && QT_QPA_PLATFORM=offscreen QTWEBENGINE_DISABLE_SANDBOX=1 "$PY" -c '
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWebEngineWidgets import QWebEngineView
app = QApplication([])
QWebEngineView()
' 2>&1 >/dev/null | grep -iE "error|cannot open|could not" | head -3 || true)"
MISSING=""
for pkg in libxcb-cursor0 libxkbcommon-x11-0 libnss3; do
	dpkg -s "$pkg" >/dev/null 2>&1 || MISSING="$MISSING $pkg"
done
if [ -n "$MISSING" ] || [ -n "$QT_ERR" ]; then
	echo "   Qt 窗口可能打不开。${MISSING:+缺系统库：sudo apt install$MISSING}"
	[ -n "$QT_ERR" ] && echo "   $QT_ERR"
else
	echo "   正常"
fi
if [ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" = "1" ]; then
	echo "   本机限制了非特权用户命名空间（Ubuntu 24.04 默认），启动时会自动关闭内嵌浏览器沙箱，只加载本机页面"
fi

echo "== 5/5 加应用菜单和桌面图标"
if [ -z "${NO_DESKTOP:-}" ]; then
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
cat > "$APPS/teleop-bench.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=遥操测试台
Name[en]=Teleop Bench
Comment=遥操测试自动化：指标台账、模式切换压测、长测看护、视频延迟、LBMotion 推杆
Exec=env BENCH_PYTHON=$PY $HERE/teleop-bench.sh
Icon=$HERE/assets/teleop-bench.svg
Terminal=false
Categories=Development;
EOF
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
for desk in "$DESK" "$HOME/Desktop" "$HOME/桌面"; do
	if [ -n "$desk" ] && [ -d "$desk" ]; then
		cp "$APPS/teleop-bench.desktop" "$desk/"
		chmod +x "$desk/teleop-bench.desktop"
		gio set "$desk/teleop-bench.desktop" metadata::trusted true 2>/dev/null || true
		break
	fi
done
fi

echo "== 自检"
cd "$HERE/backend"
"$PY" -m pytest -q tests/ 2>&1 | tail -1
echo "装好了：应用菜单或桌面上点“遥操测试台”打开。"
[ -f "$HOME/.ssh/oli_robot_manager_ed25519" ] || echo "第一次连机器人前：连上机器人热点，运行 bash $HERE/robot-access.sh"
