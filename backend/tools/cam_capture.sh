#!/usr/bin/env bash
# 两台 USB 双目相机同时录像，时间戳用电脑时钟（-use_wallclock_as_timestamps），分析时两路直接相减。
# 用法：bash cam_capture.sh <名称> [秒数=30] [分辨率=2560x720] [相机A=/dev/video4] [相机B=/dev/video6]
#   相机 A 拍屏幕上的闪烁块，相机 B 拍 Pico 镜片。输出 <名称>_A.mkv、<名称>_B.mkv（MJPEG 原样保存）。
# 校准：两台都对着屏幕拍一次（名称写 calib），得到两台相机自身的时间差。
set -euo pipefail
NAME="${1:?用法：bash cam_capture.sh <名称> [秒数] [分辨率] [相机A] [相机B]}"
DUR="${2:-30}"
SIZE="${3:-2560x720}"
A="${4:-/dev/video4}"
B="${5:-/dev/video6}"
FF="${FFMPEG:-$HOME/miniforge3/envs/vis/lib/python3.10/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2}"
rec() {
	"$FF" -hide_banner -loglevel warning -f v4l2 -input_format mjpeg -video_size "$SIZE" -framerate 60 \
		-use_wallclock_as_timestamps 1 -t "$DUR" -i "$1" -c copy -copyts -y "$2"
}
echo "录 $DUR s：A=$A → ${NAME}_A.mkv，B=$B → ${NAME}_B.mkv"
rec "$A" "${NAME}_A.mkv" &
PA=$!
rec "$B" "${NAME}_B.mkv" &
PB=$!
wait $PA $PB
for f in "${NAME}_A.mkv" "${NAME}_B.mkv"; do
	# ffmpeg 7 复制流时进度里没有 frame=，改成逐包数行
	n=$("$FF" -hide_banner -loglevel error -i "$f" -c copy -f framemd5 - 2>/dev/null | grep -vc '^#' || true)
	echo "$f：$n 帧，约 $((n / DUR)) fps"
done
