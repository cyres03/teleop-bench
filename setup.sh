#!/usr/bin/env bash
# 遥操测试台一键安装 / 更新（Ubuntu 22.04、24.04）。
# 用法：按 Ctrl+Alt+T 打开终端，粘贴下面这一行，回车：
#   wget -qO- https://raw.githubusercontent.com/cyres03/teleop-bench/main/setup.sh | bash
# 做的事：装系统组件（要输一次电脑开机密码）→ 下载软件到 ~/teleop-bench（已有就更新）→ 运行 install.sh → 桌面出现“遥操测试台”图标。
set -euo pipefail
REPO="https://github.com/cyres03/teleop-bench.git"
DIR="${TELEOP_BENCH_DIR:-$HOME/teleop-bench}"

say() { printf '\n\033[1;32m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m出错了：%s\033[0m\n把这个终端窗口截图发给 Cyres。\n' "$*"; exit 1; }

. /etc/os-release 2>/dev/null || true
[ "${ID:-}" = "ubuntu" ] || die "只支持 Ubuntu 22.04 / 24.04，这台是 ${PRETTY_NAME:-未知系统}"

say "1/3 装系统组件"
PKGS="git python3-venv python3-pip libxcb-cursor0 libxkbcommon-x11-0 libnss3 openssh-client network-manager"
MISSING=""
for p in $PKGS; do dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"; done
if [ -n "$MISSING" ] && [ -z "${SKIP_APT:-}" ]; then
	echo "要装：$MISSING"
	echo "下面会问“[sudo] password”，输入这台电脑的开机密码（输入时屏幕上不显示，输完直接回车）："
	sudo apt-get update -qq || die "apt 更新失败：密码输错了，或者网络不通"
	sudo apt-get install -y -qq $MISSING || die "系统组件没装上"
else
	echo "已经都有了${MISSING:+（跳过：$MISSING）}"
fi

say "2/3 下载软件到 $DIR"
if [ -d "$DIR/.git" ]; then
	git -C "$DIR" pull --ff-only -q || die "更新失败：$DIR 里有人改过文件"
	echo "已更新到最新版"
else
	[ -e "$DIR" ] && die "$DIR 已经存在但不是软件目录，先改名或删掉"
	git clone -q --depth 1 "$REPO" "$DIR" || die "下载失败，检查网络能不能打开 github.com"
fi

say "3/3 安装（第一次要下载约 300 MB，3–10 分钟）"
bash "$DIR/install.sh" || die "安装失败"

say "装好了"
echo "桌面上双击“遥操测试台”打开（第一次双击如果提示，选“允许运行 / Allow Launching”）。"
echo "以后要更新，再运行一次同样那行命令。"
