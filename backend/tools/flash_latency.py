#!/usr/bin/env python3
"""用两台相机的录像算端到端视频延迟（闪烁块互相关法）。

屏幕打开 flash.html，左边大块按 150–600 ms 的随机间隔黑白翻转。
相机 A 拍屏幕，相机 B 拍 Pico 镜片（镜片里是机器人头部相机拍到的屏幕）。
两路录像都用电脑时钟打时间戳（cam_capture.sh），B 里每一次翻转减去 A 里对应的那次翻转，就是延迟。
两台同型号相机自身的采集延迟基本相同，相减后抵消；剩下的差用 calib 录像校准（两台都对着屏幕拍）。
单次翻转的读数受帧间隔量化（30 fps 时 ±17 ms），翻转时刻随机，量化误差在均值里抵消，所以延迟以均值为准。

用法：
  校准：python flash_latency.py calib_A.mkv calib_B.mkv
  测量：python flash_latency.py tx_A.mkv tx_B.mkv --calib <校准输出的均值 ms> --csv tx.csv
  双目相机左右并排，默认只取左眼（--eye left）；闪烁块区域默认自动找（画面里亮度随时间变化最大的那片），
  也可以用 --roi-a / --roi-b x,y,w,h 指定（按缩小后 160 像素宽的坐标）。
"""
import argparse
import os
import re
import subprocess
import sys

import numpy as np

FF = os.environ.get('FFMPEG', os.path.expanduser(
    '~/miniforge3/envs/vis/lib/python3.10/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2'))
W = 160


def decode(path, eye):
    crop = {'left': 'crop=iw/2:ih:0:0,', 'right': 'crop=iw/2:ih:iw/2:0,', 'full': ''}[eye]
    cmd = [FF, '-hide_banner', '-loglevel', 'info', '-copyts', '-i', path, '-fps_mode', 'passthrough',
           '-vf', f'{crop}scale={W}:-2,format=gray,showinfo', '-f', 'rawvideo', '-pix_fmt', 'gray', '-']
    p = subprocess.run(cmd, capture_output=True)
    ts = [float(x) for x in re.findall(rb'pts_time:([0-9.]+)', p.stderr)]
    m = re.search(rb'Stream.*?(\d{2,5})x(\d{2,5})', p.stderr.split(b'Output')[-1])
    w, h = int(m.group(1)), int(m.group(2))
    raw = np.frombuffer(p.stdout, np.uint8)
    n = min(len(ts), raw.size // (w * h))
    if n < 20:
        sys.exit(f'{path}：只解出 {n} 帧')
    return np.array(ts[:n]), raw[:n * w * h].reshape(n, h, w)


def signal(frames, roi):
    """返回亮度信号和区域。flash.html 左右两半反相翻转：自动找区域时，取与变化最大的像素同相、反相的两组像素，
    信号 = 同相组均值 − 反相组均值（环境光、自动曝光的整体变化相减抵消）；只拍到一半时就是那一半的均值。"""
    n = len(frames)
    if roi:
        x, y, w, h = roi
        return frames[:, y:y + h, x:x + w].reshape(n, -1).mean(1), roi
    f = frames.reshape(n, -1).astype(np.float32)
    f -= f.mean(0)
    sd = f.std(0)
    ref = f[:, sd.argmax()]
    corr = (f * ref[:, None]).mean(0) / (sd * ref.std() + 1e-6)
    pos, neg = corr > 0.6, corr < -0.6
    h, w = frames.shape[1:]
    ys, xs = np.nonzero(pos.reshape(h, w))
    box = (int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1))
    s = f[:, pos].mean(1)
    if neg.sum() > 0.2 * pos.sum():
        s = s - f[:, neg].mean(1)
        box = box + ('反相', int(neg.sum()))
    return s, box


def edges(t, s):
    lo, hi = np.percentile(s, 5), np.percentile(s, 95)
    if hi - lo < 10:
        sys.exit('闪烁块亮度变化太小（<10 灰度），检查相机是否拍到闪烁块')
    v = (s - lo) / (hi - lo)
    out, state = [], v[0] > 0.5
    for i in range(1, len(v)):
        if not state and v[i] > 0.65 or state and v[i] < 0.35:
            # 在相邻两帧之间按 0.5 线性插值出翻转时刻
            j = i
            while j > 0 and (v[j - 1] > 0.5) != state:
                j -= 1
            j = max(j, 1)
            a, b = v[j - 1], v[j]
            frac = (0.5 - a) / (b - a) if b != a else 0.5
            out.append((t[j - 1] + frac * (t[j] - t[j - 1]), not state))
            state = not state
    return out


def coarse_lag(ea, eb, max_lag=2.0):
    t0 = min(ea[0][0], eb[0][0])
    t1 = max(ea[-1][0], eb[-1][0])
    grid = np.arange(t0, t1, 0.001)

    def square(e):
        x = np.zeros_like(grid)
        for (ta, up), nxt in zip(e, e[1:] + [(t1, None)]):
            x[(grid >= ta) & (grid < nxt[0])] = 1 if up else -1
        return x
    xa, xb = square(ea), square(eb)
    lags = np.arange(-200, int(max_lag * 1000))
    best = max(lags, key=lambda L: abs(np.dot(xa[:len(xa) - L] if L >= 0 else xa[-L:], xb[L:] if L >= 0 else xb[:len(xb) + L])))
    c = np.dot(xa[:len(xa) - best] if best >= 0 else xa[-best:], xb[best:] if best >= 0 else xb[:len(xb) + best])
    return best / 1000.0, c < 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('a')
    ap.add_argument('b')
    ap.add_argument('--eye', default='left', choices=['left', 'right', 'full'])
    ap.add_argument('--roi-a')
    ap.add_argument('--roi-b')
    ap.add_argument('--calib', type=float, default=0.0, help='校准得到的 B−A 均值，ms')
    ap.add_argument('--tol', type=float, default=70, help='逐次配对容差，ms（小于翻转最短间隔的一半）')
    ap.add_argument('--csv')
    a = ap.parse_args()
    roi = lambda s: tuple(int(v) for v in s.split(',')) if s else None
    ta, fa = decode(a.a, a.eye)
    tb, fb = decode(a.b, a.eye)
    if ta[0] < 1e6 or tb[0] < 1e6:
        print('注意：时间戳不是电脑时钟（录像时要加 -use_wallclock_as_timestamps 1 -copyts），两路无法直接相减')
    sa, ra = signal(fa, roi(a.roi_a))
    sb, rb = signal(fb, roi(a.roi_b))
    ea, eb = edges(ta, sa), edges(tb, sb)
    lag, inv = coarse_lag(ea, eb)
    if inv:
        eb = [(t, not u) for t, u in eb]
    if lag < -0.03:
        print('注意：B 比 A 早，A、B 可能接反了（A 应拍屏幕、B 拍 Pico），把两个文件换个顺序再跑')
    pairs = []
    for t_b, up in eb:
        cand = [(abs(t_b - lag - t_a), t_a) for t_a, u in ea if u == up]
        if cand:
            d, t_a = min(cand)
            if d * 1000 <= a.tol:
                pairs.append((t_a, t_b, (t_b - t_a) * 1000 - a.calib))
    fps = lambda t: (len(t) - 1) / (t[-1] - t[0])
    print(f'A：{len(ta)} 帧 {fps(ta):.1f} fps，闪烁块 {ra}，翻转 {len(ea)} 次')
    print(f'B：{len(tb)} 帧 {fps(tb):.1f} fps，闪烁块 {rb}，翻转 {len(eb)} 次{"（亮暗相反，已翻转）" if inv else ""}')
    print(f'互相关粗估 B−A：{lag * 1000:.0f} ms；配上 {len(pairs)} 次翻转' + (f'，已减校准 {a.calib:.1f} ms' if a.calib else ''))
    if not pairs:
        sys.exit('没有配上的翻转')
    d = np.array([p[2] for p in pairs])
    q = 500 / fps(ta) + 500 / fps(tb)
    print(f'延迟 ms：均值 {d.mean():.1f}（±{2 * d.std() / np.sqrt(len(d)):.1f}，95% 置信），中位数 {np.median(d):.1f}，标准差 {d.std():.1f}，'
          f'P5 {np.percentile(d, 5):.1f}，P95 {np.percentile(d, 95):.1f}，最小 {d.min():.1f}，最大 {d.max():.1f}')
    print(f'单次读数受两路帧间隔量化，误差最大 ±{q:.0f} ms；翻转时刻随机，量化误差在均值里抵消，延迟以均值为准')
    if a.csv:
        with open(a.csv, 'w') as f:
            f.write('A翻转时刻,B翻转时刻,延迟ms\n')
            for t_a, t_b, x in pairs:
                f.write(f'{t_a:.4f},{t_b:.4f},{x:.1f}\n')


if __name__ == '__main__':
    main()
