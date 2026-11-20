#!/usr/bin/env bash
# 一键切换遥操视频编码（Oli / Tron2，单目 / 双目，近场 / 远程）。
# 用法：在机器人主控上运行  bash video_codec.sh
#   Oli 必须在主控 1.2（ssh limx@10.192.1.2）上运行：1.3 上的 /opt/limx 是 1.2 的只读共享，在 1.3 上改不了。
# 做的事：选机型和相机 → 显示当前编码 → 选要改的链路和编码 → 确认 → 当天第一次改先备份 → 改配置 → 重启视频服务。
# 配置文件：Oli 单目 oli_edu_mros.yaml，Tron2 单目 tron2.yaml，双目 dual_camera_gmsl.yaml（Oli、Tron2 共用）。
# 腾讯云 TRRO 的编码实际看 trro/config.json 每路视频的 codec（0=H264，1=H265），选腾讯云时两个文件一起改。
# 新刷的固件包会把这些配置文件重置，刷包后要重新切。
set -euo pipefail

CFG_DIR="${CFG_DIR:-/opt/limx/install/etc/livekit/configs}"
AUTO="${AUTO:-/opt/limx/install/etc/autolaunch/robot-app.yaml}"
BK_ROOT="${BK_ROOT:-$HOME/video_codec_backup}"
VIDEO_HOST="${VIDEO_HOST:-10.192.1.3}"
NODE=pico_teleop_server
SETUP=/opt/limx/install/setup.bash

die() { echo "错误：$*" >&2; exit 1; }

# 读一个选项：choose "提示" 选项1 选项2 ...，结果放在 $REPLY_VAL
choose() {
	local prompt=$1; shift
	local i=1 opt
	echo
	echo "$prompt"
	for opt in "$@"; do echo "  $i) $opt"; i=$((i + 1)); done
	while true; do
		read -r -p "输入序号: " n
		if [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -ge 1 ] && [ "$n" -le $# ]; then
			REPLY_VAL="${!n}"
			return
		fi
		echo "  请输入 1–$# 之间的数字"
	done
}

# ---------- 读配置 ----------
local_codec() {  # local 段里的 codec，没写就是 auto
	awk '/^local:/{s=1;next} /^[^[:space:]#]/{s=0} s && /^  codec:/{sub(/^  codec:[[:space:]]*/,""); sub(/[[:space:]#].*/,""); print; f=1; exit} END{if(!f) print "auto"}' "$1"
}
remote_get() {  # remote_get 文件 livekit|trro enabled|codec
	awk -v sec="$2" -v key="$3" '
		/^remote:/{r=1;next} /^[^[:space:]#]/{r=0}
		r && /^  [A-Za-z_]+:/{cur=$1; sub(":","",cur); next}
		r && cur==sec && $0 ~ "^    " key ":" {v=$0; sub("^    " key ":[[:space:]]*","",v); sub(/[[:space:]#].*/,"",v); gsub(/"/,"",v); print v; exit}
	' "$1"
}
remote_provider() {
	local lk tr
	lk=$(remote_get "$1" livekit enabled); tr=$(remote_get "$1" trro enabled)
	if [ "$tr" = true ] && [ "$lk" = true ]; then echo 冲突
	elif [ "$tr" = true ]; then echo trro
	elif [ "$lk" = true ]; then echo livekit
	else echo 无; fi
}
trro_json_codec() {
	local j="$CFG_DIR/trro/config.json"
	[ -f "$j" ] || { echo "无此文件"; return; }
	local c
	c=$(grep -oE '"codec"[[:space:]]*:[[:space:]]*-?[0-9]+' "$j" | grep -oE -- '-?[0-9]+$' | sort -u | tr '\n' ' ')
	case "$c" in "0 ") echo h264 ;; "1 ") echo h265 ;; *) echo "各路不一致($c)" ;; esac
}
in_use() { sed -n 's/^[[:space:]]*CONFIG_FILE:[[:space:]]*//p' "$AUTO" 2>/dev/null | head -1; }

show() {
	local f=$1 p
	p=$(remote_provider "$f")
	echo "  近场编码：$(local_codec "$f")"
	case "$p" in
		livekit) local v; v=$(remote_get "$f" livekit codec); echo "  远程：阿里云 LiveKit，编码 ${v:-未写（程序默认）}" ;;
		trro) echo "  远程：腾讯云 TRRO，config.json 编码 $(trro_json_codec)（yaml 里 trro.codec=$(remote_get "$f" trro codec)，对腾讯云不起作用）" ;;
		*) echo "  远程：$p" ;;
	esac
}

# ---------- 改配置（输出到 stdout，原文件不动） ----------
edit_local() {  # edit_local 文件 编码
	awk -v c="$2" '
		/^local:/{s=1;print;next} /^[^[:space:]#]/{if(s&&!done){print "  codec: " c; done=1} s=0}
		s && /^  codec:/ && !done {sub(/codec:[[:space:]]*[^[:space:]#]+/,"codec: " c); done=1}
		{print}
		END{if(s&&!done) print "  codec: " c}
	' "$1"
}
edit_remote_codec() {  # edit_remote_codec 文件 livekit|trro 编码：有 codec 行就改，没有就加在 enabled 行后面
	local has; has=$(remote_get "$1" "$2" codec)
	awk -v sec="$2" -v c="$3" -v has="$has" '
		/^remote:/{r=1;print;next} /^[^[:space:]#]/{r=0}
		r && /^  [A-Za-z_]+:/{cur=$1; sub(":","",cur)}
		r && cur==sec && has!="" && /^    codec:/ {sub(/codec:[[:space:]]*[^[:space:]#]+/,"codec: " c)}
		{print}
		r && cur==sec && has=="" && /^    enabled:/ && !done {print "    codec: " c; done=1}
	' "$1"
}
edit_trro_json() {  # edit_trro_json 编码
	local n; n=$([ "$1" = h265 ] && echo 1 || echo 0)
	sed -E "s/(\"codec\"[[:space:]]*:[[:space:]]*)-?[0-9]+/\1$n/g" "$CFG_DIR/trro/config.json"
}

# ---------- 写入、备份、重启 ----------
can_write() {
	if mount | grep -E " on /opt(/limx)? type cifs" >/dev/null 2>&1; then
		die "这台板子上的 /opt/limx 是共享的只读目录，改不了。Oli 请到主控 1.2 上运行：ssh limx@10.192.1.2"
	fi
}
backup() {  # 当天第一次改某个文件前备份
	local f=$1 d; d="$BK_ROOT/$(date +%Y%m%d)"
	mkdir -p "$d"
	[ -f "$d/$(basename "$f")" ] || { cp -p "$f" "$d/"; echo "  已备份 $(basename "$f") → $d/"; }
}
install_file() {  # install_file 新内容文件 目标
	backup "$2"
	if [ -w "$2" ] && [ -z "${USE_SUDO:-}" ]; then cp "$1" "$2"; else sudo install -m 644 -o root -g root "$1" "$2"; fi
	cmp -s "$1" "$2" || die "写入后核对不一致：$2"
}
restart() {
	[ -n "${NO_RESTART:-}" ] && { echo "  （NO_RESTART：跳过重启）"; return; }
	echo
	echo "== 重启视频服务 $NODE"
	local out
	out=$(bash -c "source $SETUP >/dev/null 2>&1; export MROS_IP_LIST=10.192.1.x; timeout 40 mrosnode restart $NODE" 2>&1 || true)
	echo "  本机：${out:-无输出}"
	if echo "$out" | grep -qiE "unknow|fail|error|not found"; then
		if ping -c 1 -W 2 "$VIDEO_HOST" >/dev/null 2>&1; then
			echo "  视频服务不在本板子上，到 $VIDEO_HOST 上重启（输入 guest 密码）"
			ssh -t "guest@$VIDEO_HOST" "source $SETUP >/dev/null 2>&1; export MROS_IP_LIST=10.192.1.x; mrosnode restart $NODE" \
				|| echo "  重启失败：手动到视频服务所在的板子上运行  mrosnode restart $NODE，或重启机器人"
		else
			echo "  重启失败：手动到视频服务所在的板子上运行  mrosnode restart $NODE，或重启机器人"
		fi
	fi
	echo "  重启后 Pico 要重新连接；头显不支持所选编码时会自动退回 JPEG，在 Pico 上确认画面正常。"
}

restore() {
	[ -d "$BK_ROOT" ] || die "没有备份目录 $BK_ROOT"
	mapfile -t days < <(ls -1 "$BK_ROOT" | sort)
	[ ${#days[@]} -gt 0 ] || die "没有备份"
	choose "用哪一天的备份还原（最早的那份是第一次改之前的原始配置）？" "${days[@]}"
	local d="$BK_ROOT/$REPLY_VAL" f
	for f in "$d"/*; do
		case "$(basename "$f")" in
			config.json) tgt="$CFG_DIR/trro/config.json" ;;
			robot-app.yaml) tgt="$AUTO" ;;
			*) tgt="$CFG_DIR/$(basename "$f")" ;;
		esac
		echo "  还原 $tgt"
		if [ -w "$tgt" ] && [ -z "${USE_SUDO:-}" ]; then cp "$f" "$tgt"; else sudo install -m 644 -o root -g root "$f" "$tgt"; fi
	done
	restart
}

# ---------- 主流程 ----------
main() {
	[ -d "$CFG_DIR" ] || die "找不到 $CFG_DIR，这台机器没有装遥操视频服务"
	can_write
	TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

	echo "当前视频服务用的配置：$(in_use || echo 未知)"
	choose "要做什么？" "切换编码" "还原备份"
	[ "$REPLY_VAL" = "还原备份" ] && { restore; return; }

	choose "机型？" "Oli" "Tron2"; local model=$REPLY_VAL
	choose "头部相机？" "单目" "双目"; local cam=$REPLY_VAL
	local name
	if [ "$cam" = 双目 ]; then name=dual_camera_gmsl.yaml
	elif [ "$model" = Oli ]; then name=oli_edu_mros.yaml
	else name=tron2.yaml; fi
	local f="$CFG_DIR/$name"
	[ -f "$f" ] || die "找不到 $f"

	echo
	echo "配置文件：$f"
	show "$f"
	local cur; cur=$(in_use || true)
	local switch_cfg=""
	if [ -n "$cur" ] && [ "$cur" != "$name" ]; then
		echo
		echo "注意：视频服务现在用的是 $cur，不是 $name，只改 $name 不会生效。"
		choose "要把视频服务改成用 $name 吗？（要和机器上实际装的相机一致）" "改成用 $name" "不改，只改 $name 的编码"
		[ "$REPLY_VAL" = "改成用 $name" ] && switch_cfg=1
	fi

	choose "改哪条链路？" "近场（Pico 连机器人热点）" "远程"
	local link=$REPLY_VAL codec new prov
	cp "$f" "$TMP/new.yaml"
	if [[ "$link" == 近场* ]]; then
		choose "近场编码？" "jpeg（走 QUIC）" "h264（点对点 WebRTC）" "h265（点对点 WebRTC）" "auto（按头显能力自动选）"
		codec=${REPLY_VAL%%（*}
		edit_local "$f" "$codec" > "$TMP/new.yaml"
		[ "$(local_codec "$TMP/new.yaml")" = "$codec" ] || die "改完读回来不是 $codec，配置文件格式和预期不同，已停止"
		new="近场 $codec"
	else
		prov=$(remote_provider "$f")
		case "$prov" in livekit|trro) ;; *) die "远程配置状态：$prov，先找开发确认远程用哪家" ;; esac
		choose "远程编码？（当前远程用 $([ "$prov" = trro ] && echo 腾讯云 TRRO || echo 阿里云 LiveKit)）" "h264" "h265"
		codec=$REPLY_VAL
		edit_remote_codec "$f" "$prov" "$codec" > "$TMP/new.yaml"
		[ "$(remote_get "$TMP/new.yaml" "$prov" codec)" = "$codec" ] || die "改完读回来不是 $codec，配置文件格式和预期不同，已停止"
		if [ "$prov" = trro ]; then
			[ -f "$CFG_DIR/trro/config.json" ] || die "腾讯云要改 $CFG_DIR/trro/config.json，但文件不存在"
			edit_trro_json "$codec" > "$TMP/config.json"
			grep -qE '"codec"' "$TMP/config.json" || die "trro/config.json 里没有 codec，已停止"
		fi
		new="远程 $([ "$prov" = trro ] && echo 腾讯云 || echo 阿里云) $codec"
	fi

	echo
	echo "将要："
	[ -n "$switch_cfg" ] && echo "  - 视频服务改用 $name（原来是 $cur）"
	echo "  - $name 改成 $new"
	[ -f "$TMP/config.json" ] && echo "  - trro/config.json 每路视频 codec 改成 $codec"
	echo "  - 重启 $NODE（Pico 会断开，要重新连接）"
	read -r -p "确认执行？输入 y 继续: " ok
	[ "$ok" = y ] || { echo "已取消，没有改动"; return; }

	if cmp -s "$TMP/new.yaml" "$f"; then echo "  $name 已经是这个编码"; else install_file "$TMP/new.yaml" "$f"; fi
	[ -f "$TMP/config.json" ] && { cmp -s "$TMP/config.json" "$CFG_DIR/trro/config.json" || install_file "$TMP/config.json" "$CFG_DIR/trro/config.json"; }
	if [ -n "$switch_cfg" ]; then
		sed -E "s/^([[:space:]]*CONFIG_FILE:[[:space:]]*).*/\1$name/" "$AUTO" > "$TMP/robot-app.yaml"
		install_file "$TMP/robot-app.yaml" "$AUTO"
		echo "  视频服务配置已改成 $name；mrosnode 重启不一定重新读 CONFIG_FILE，Pico 画面不对时重启机器人"
	fi
	echo
	echo "改完后："
	show "$f"
	restart
}

main "$@"
