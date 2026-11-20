#!/usr/bin/env bash
# 授权本机登录当前连着的机器人（每台机器人第一次用测试台前运行一次）。
# 用法：先连上机器人热点（HU_ 开头），再运行 bash robot-access.sh
# 做的事：
#   1. 本机没有密钥就生成 ~/.ssh/oli_robot_manager_ed25519（只在本机，不上传任何私钥）；
#   2. 把这台机器人 1.2、1.3 的主机指纹记进 ~/.ssh/known_hosts_teleop_bench（测试台只接受记录过的机器人）；
#   3. 把本机公钥加到 limx@10.192.1.2 和 guest@10.192.1.3，各要输一次该机器人的登录密码（脚本不保存密码）。
set -euo pipefail
KEY="$HOME/.ssh/oli_robot_manager_ed25519"
KH="$HOME/.ssh/known_hosts_teleop_bench"
MAIN="${MAIN_HOST:-10.192.1.2}"
VIDEO="${VIDEO_HOST:-10.192.1.3}"

mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
if [ ! -f "$KEY" ]; then
	echo "== 生成本机密钥 $KEY"
	ssh-keygen -q -t ed25519 -N "" -C "teleop-bench@$(hostname)" -f "$KEY"
fi

echo "== 检查是否连着机器人"
ping -c 1 -W 2 "$MAIN" >/dev/null 2>&1 || { echo "ping 不通 $MAIN：先连上机器人热点（HU_ 开头）"; exit 1; }

for host in "$MAIN" "$VIDEO"; do
	echo "== 记录 $host 的主机指纹"
	# 同一个 IP 每台机器人的指纹都不同：测试台按“出现在任一记录里”接受，所以这里追加，不覆盖
	ssh-keyscan -T 5 -t ed25519,ecdsa,rsa "$host" 2>/dev/null >> "$KH" || echo "   $host 没有响应，跳过"
	sort -u "$KH" -o "$KH"
done

auth() {
	local user=$1 host=$2
	echo "== 授权 $user@$host（输入这台机器人的 $user 密码）"
	ssh-copy-id -i "$KEY.pub" -o IdentitiesOnly=yes -o UserKnownHostsFile="$KH" -o StrictHostKeyChecking=yes "$user@$host"
	ssh -i "$KEY" -o IdentitiesOnly=yes -o UserKnownHostsFile="$KH" -o StrictHostKeyChecking=yes -o BatchMode=yes -o ConnectTimeout=8 \
		"$user@$host" 'sed -n "s/^sn: *//p" /opt/limx/imptdata/sn.yaml 2>/dev/null; echo' | head -1 | sed "s/^/   免密登录成功，SN：/"
}
auth limx "$MAIN"
auth guest "$VIDEO" || echo "   1.3 授权失败：只影响“视频延迟”页切换编码，其他功能不受影响"
echo "完成：这台机器人可以用测试台了。换另一台机器人时再运行一次。"
