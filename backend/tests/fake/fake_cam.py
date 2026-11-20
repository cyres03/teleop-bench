"""假相机：生成一段闪烁块录像，代替 v4l2 采集，供验收测试用。

时间轴和翻转序列由环境变量 FAKE_T0 决定（同一次测试里 A、B 共用），
FAKE_DELAY_<设备名> 给某台相机加延迟（秒），用来模拟“镜片里的画面”。
"""
import os
import subprocess
import sys
import time

import numpy as np

dev, out, dur, ff = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
name = os.path.basename(dev)
if "Dead" in name:  # 模拟录像途中掉线的相机
    print(f"[video4linux2] {dev}: No such device", file=sys.stderr)
    sys.exit(1)
delay = float(os.environ.get(f"FAKE_DELAY_{name}", "0"))
_dir = os.environ.get("FAKE_DELAY_DIR")  # 界面演练用：运行中改这个目录下的文件就能换延迟
if _dir and os.path.exists(os.path.join(_dir, name)):
    delay = float(open(os.path.join(_dir, name)).read().strip() or 0)
fps = 30 if name.endswith("A") else 60
t0 = float(os.environ["FAKE_T0"])
rng = np.random.default_rng(7)
edges = [t0 - 5]
while edges[-1] < t0 + 300:
    edges.append(edges[-1] + rng.uniform(0.15, 0.6))
start = time.time()
noise = np.random.default_rng(len(name) + ord(name[-1]))


def level(t):
    return 230 if (np.searchsorted(edges, t) - 1) % 2 == 0 else 20


w, h = 640, 240
p = subprocess.Popen([ff, "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}",
                      "-r", str(fps), "-i", "-", "-output_ts_offset", str(start), "-c:v", "mjpeg", "-q:v", "5", "-y", out],
                     stdin=subprocess.PIPE)
for i in range(int(dur * fps)):
    v = level(start + i / fps - delay)
    f = np.full((h, w), 60, np.float32)
    f[60:180, 20:150] = v
    f[60:180, 170:300] = 250 - v
    f[60:180, 340:470] = v
    f[60:180, 490:620] = 250 - v
    p.stdin.write(np.clip(f + noise.normal(0, 4, f.shape), 0, 255).astype(np.uint8).tobytes())
p.stdin.close()
sys.exit(p.wait())
