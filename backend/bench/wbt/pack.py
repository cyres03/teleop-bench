#!/usr/bin/env python3
"""全身遥操回放包：做包（电脑上）和播放（主控上）。只用标准库，主控上和 bag_extract.py、rosbag_lite.py 放一起直接跑。

做包：python3 pack.py make <原始录包> <输出包> [--clip 起 止]（秒，相对跟随开始；不写就是整段跟随）[--half]
  1. 只留跟随段（/teleop/state 最长一段 1），加 /vr_cmd 和人体 /tf（pico_ 和 /vr_body/*，不带机器人自己的 /tf）。
     --half：半身（VR部分动捕）工作包，只留 /vr_cmd（手柄位姿、摇杆、扳机）。
  2. /vr_cmd 里的 A/B/X/Y 全部清掉：全身模式下单按 A/X/Y 会切控制器，回放只要姿态、握把和扳机。
  3. 开头补 LEAD 秒“进入遥操”：人保持第一帧姿势，先按一次 RG+A（进入遥操模式），再按住第一帧的握把
     （握把是数据总开关，按住就跟随）。结尾补 TAIL 秒：保持最后一帧，RG+B（回 Walk）后全部松开。
     /vr_cmd 的 seq 前后接上，不触发 seq 跳变。
播放：python3 pack.py play <回放包> [--delay 秒，默认按包大小估]
  录包时间平移到“现在 + delay”，header.stamp 设成新的录包时间，写成 <回放包>.now.bag，再 exec mrosplay。
"""
from __future__ import annotations

import os
import re
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from ..lbm import bag_extract as be  # 包内导入（电脑上）
    from ..lbm import rosbag_lite as rl
except ImportError:  # 主控上平铺在一个目录
    import bag_extract as be
    import rosbag_lite as rl

HUMAN_TF = re.compile(r"^(pico_|/vr_body/)")
LEAD = 2.5           # 开头补多少秒
A_AT = (0.6, 1.0)    # RG+A 按下区间（相对补段开头）
GRIP_AT = 1.6        # 从这时起按住握把
TAIL = 1.0           # 结尾补多少秒：先 RG+B（回 Walk），再全部松开
B_S = 0.3            # 结尾 RG+B 按多久
TF_HZ = 45.0
# VRState 正文（header 之后）各字段偏移：eyePose/l/r 各 16 个 float32，leftJS 2 个 float32 ……
VR = {"leftGrip": 204, "LG": 210, "X": 211, "Y": 212, "rightGrip": 225, "RG": 231, "A": 232, "B": 233}
VR_LEN = 234


def _body(data: bytes) -> int:
    (ln,) = struct.unpack_from("<I", data, 12)
    return 16 + ln


def vr_get(data: bytes, key: str):
    off = _body(data) + VR[key]
    return struct.unpack_from("<f", data, off)[0] if key.endswith("Grip") else bool(data[off])


def vr_set(data: bytes, **kw) -> bytes:
    b = bytearray(data)
    base = _body(b)
    if len(b) != base + VR_LEN:
        raise ValueError("/vr_cmd 长度不对，不是 teleop_msgs/VRState")
    for k, v in kw.items():
        if k.endswith("Grip"):
            struct.pack_into("<f", b, base + VR[k], float(v))
        else:
            b[base + VR[k]] = 1 if v else 0
    return bytes(b)


def set_header(data: bytes, off: int, sec: int, nsec: int, seq: int | None = None) -> None:
    if seq is not None:
        struct.pack_into("<I", data, off, seq & 0xFFFFFFFF)
    struct.pack_into("<II", data, off + 4, sec, nsec)


def tf_headers(data: bytes) -> list[int]:
    """tf/tfMessage 里每个变换 header 的偏移。"""
    (n,) = struct.unpack_from("<I", data, 0)
    off, out = 4, []
    for _ in range(n):
        out.append(off)
        off += 12
        (ln,) = struct.unpack_from("<I", data, off)
        off += 4 + ln
        (ln,) = struct.unpack_from("<I", data, off)
        off += 4 + ln + 56
    return out


def _ns(sec, nsec):
    return sec * 1_000_000_000 + nsec


def _split(ns):
    return ns // 1_000_000_000, ns % 1_000_000_000


def follow_window(src: str) -> tuple[float, float]:
    st = [(t, (m["data"][1] if len(m["data"]) > 1 else m["data"][0])) for _, t, m in rl.Bag(src).read(["/teleop/state"])]
    best, a = None, None
    for t, v in st + [(None, 0)]:
        if v and a is None:
            a = t
        elif not v and a is not None:
            end = t if t is not None else st[-1][0]
            if best is None or end - a > best[1] - best[0]:
                best = (a, end)
            a = None
    if not best:
        raise SystemExit("录包里没有跟随段（/teleop/state 一直是 0）")
    return best


def _frozen(vid, tid, vr_frame, tf_group, start, dur, period, seq0, buttons):
    """冻结姿势补段：/vr_cmd 按 period 重复 vr_frame（按键由 buttons(相对秒) 给出），/tf 按 TF_HZ 重复 tf_group。"""
    out = []
    n = int(dur * 1e9 / period)
    for i in range(n):
        t = start + i * period
        d = bytearray(vr_set(vr_frame, **buttons(i * period / 1e9)))
        set_header(d, 0, *_split(t), seq=seq0 + i)
        out.append((vid, *_split(t), bytes(d)))
    if not tf_group:
        return out
    g0 = _ns(*tf_group[0][1:3])
    for t in range(start, start + n * period, int(1e9 / TF_HZ)):
        for m in tf_group:
            d = bytearray(m[3])
            tt = t + _ns(*m[1:3]) - g0
            for o in tf_headers(d):
                set_header(d, o, *_split(tt))
            out.append((tid, *_split(tt), bytes(d)))
    return out


def _tf_group(tf_msgs, last=False):
    """一帧骨架：开头（或结尾）1/TF_HZ 秒内的 /tf 消息（Pico 一帧骨架分几条发）。"""
    ms = tf_msgs[::-1] if last else tf_msgs
    g0 = _ns(*ms[0][1:3])
    g = [m for m in ms if abs(_ns(*m[1:3]) - g0) < 1e9 / TF_HZ]
    return sorted(g, key=lambda m: (m[1], m[2]))


def make(src: str, dst: str, clip: tuple[float, float] | None = None, half: bool = False) -> dict:
    a, b = follow_window(src)
    if clip:  # 只要跟随段里的一截，秒数相对跟随开始
        a, b = a + clip[0], min(b, a + clip[1])
    conns, msgs = be.read_raw(src, ["/vr_cmd"] if half else ["/vr_cmd", "/tf"], a, b, HUMAN_TF, absolute=True)
    ids = {topic.decode(): nid for nid, topic, _ in conns}
    if "/vr_cmd" not in ids:
        raise SystemExit("跟随段里没有 /vr_cmd")
    if not half and "/tf" not in ids:
        raise SystemExit("跟随段里缺人体 /tf：全身工作包要用 Pico 全身动捕的原始录包；半身工作包加 --half")
    vid, tid = ids["/vr_cmd"], ids.get("/tf")
    body = [(n, s_, ns, vr_set(d, X=False, Y=False, A=False, B=False) if n == vid else d) for n, s_, ns, d in msgs]
    vr = [m for m in body if m[0] == vid]
    tf_msgs = [m for m in body if m[0] == tid]
    t0, t1 = _ns(*vr[0][1:3]), _ns(*vr[-1][1:3])
    period = int((t1 - t0) / max(len(vr) - 1, 1))
    first, last = vr[0][3], vr[-1][3]
    lg, rg = vr_get(first, "LG"), vr_get(first, "RG")
    if not (lg or rg):
        lg = rg = True
    held = dict(LG=lg, leftGrip=1.0 if lg else 0, RG=rg, rightGrip=1.0 if rg else 0, A=False, B=False)
    off = dict(LG=False, leftGrip=0, RG=False, rightGrip=0, A=False, B=False)

    def lead_btn(rel):
        if A_AT[0] <= rel < A_AT[1]:
            return dict(off, RG=True, rightGrip=1.0, A=True)
        return held if rel >= GRIP_AT else off

    def tail_btn(rel):
        return dict(off, LG=True, leftGrip=1.0, RG=True, rightGrip=1.0, B=True) if rel < B_S else off

    (seq_first,) = struct.unpack_from("<I", first, 0)
    (seq_last,) = struct.unpack_from("<I", last, 0)
    n_lead = int(LEAD * 1e9 / period)
    g_first = _tf_group(tf_msgs) if tf_msgs else []
    g_last = _tf_group(tf_msgs, last=True) if tf_msgs else []
    lead = _frozen(vid, tid, first, g_first, t0 - n_lead * period, LEAD, period, seq_first - n_lead, lead_btn)
    tail = _frozen(vid, tid, last, g_last, t1 + period, TAIL, period, seq_last + 1, tail_btn)
    allm = sorted(lead + body + tail, key=lambda m: (m[1], m[2]))
    size, _ = be.write_bag(dst, conns, allm)
    span = (_ns(*allm[-1][1:3]) - _ns(*allm[0][1:3])) / 1e9
    return {"dst": dst, "mode": "半身" if half else "全身", "messages": len(allm), "seconds": round(span, 1), "follow_s": round((b - a), 1), "mb": round(size / 1e6, 1)}


def restamp(src: str, dst: str, start_ns: int) -> float:
    """整包平移到 start_ns 开始；/vr_cmd 和每个 /tf 变换的 header.stamp 都设成新的录包时间（相当于“现在收到”）。"""
    conns, msgs = be.read_raw(src, ["/vr_cmd", "/tf"])
    ids = {topic.decode(): nid for nid, topic, _ in conns}
    shift = start_ns - _ns(msgs[0][1], msgs[0][2])
    out = []
    for nid, sec, nsec, data in msgs:
        t = _split(_ns(sec, nsec) + shift)
        d = bytearray(data)
        if nid == ids.get("/vr_cmd"):
            set_header(d, 0, *t)
        elif nid == ids.get("/tf"):
            for o in tf_headers(d):
                set_header(d, o, *t)
        out.append((nid, *t, bytes(d)))
    be.write_bag(dst, conns, out)
    return (_ns(*out[-1][1:3]) - _ns(*out[0][1:3])) / 1e9


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "make":
        clip = None
        if "--clip" in sys.argv:
            i = sys.argv.index("--clip")
            clip = (float(sys.argv[i + 1]), float(sys.argv[i + 2]))
        print(make(sys.argv[2], sys.argv[3], clip, half="--half" in sys.argv))
    elif len(sys.argv) >= 3 and sys.argv[1] == "play":
        src = sys.argv[2]
        # 改时间戳要时间（主控上约 0.1 s/MB），预留出来，让第一帧的 stamp 落在开播那一刻
        delay = float(sys.argv[sys.argv.index("--delay") + 1]) if "--delay" in sys.argv else 0.5 + os.path.getsize(src) / 1e6 * 0.11
        dst = src[:-4] + ".now.bag"
        t = time.time()
        dur = restamp(src, dst, int((time.time() + delay) * 1e9))
        print(f"PLAY {dur:.1f} s，改时间戳 {time.time() - t:.1f} s", flush=True)
        os.execvp("mrosplay", ["mrosplay", "-q", "--bags", dst])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
