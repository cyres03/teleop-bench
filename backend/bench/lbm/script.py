"""半身 LBMotion 自动推杆：软件生成固定的 /vr_cmd 摇杆脚本，回放给机器人，代替人拿 Pico 推杆。

脚本时间线（70 Hz，和 Pico 一样）：
  0.6–1.0 s 按 RG+A（进入预选的遥操模式，预选要先设成 0 = VR部分动捕）
  3.0 s 起  按住两个握把（跟随，机器人才听摇杆）
  5.0 s 起  按动作表推摇杆：每个动作推满 PUSH 秒，松开等 SETTLE 秒，整组重复 reps 次
  最后     松开摇杆等 SETTLE 秒 → LG+RG+B 0.3 s（回 Walk）→ 全部松开 0.5 s
手柄和头显姿态全程固定为模板帧（11-19 全身包第一帧），双臂不动，只看行走。
摇杆方向按 660 录包核对：leftJS[1] + 前进；rightJS[0] − 左转、+ 右转；leftJS[0] − 左移（按右摇杆同样约定推定，首次真机核对）。
"""
from __future__ import annotations

import struct
from pathlib import Path

from . import bag_extract as be
from ..wbt import pack as wp

HZ = 70
LEAD_PUSH = 5.0      # 第一次推杆前的准备时间（进模式 + 握把跟随）
SETTLE = 4.0
B_S = 0.3
TEMPLATE = Path(__file__).resolve().parents[3] / "assets" / "wbt" / "前进.bag"
JS = {"leftJS": 192, "rightJS": 213}

# 每个动作：(分析里的类别, 摇杆, x, y, 推多久 s)
SCRIPTS = {
    "前进后退": {"what": "前进 10 s → 停 4 s → 后退 10 s → 停 4 s", "space": "机器人前后各留出 6 m 空地",
              "moves": [("前进", "leftJS", 0.0, 1.0, 10.0), ("后退", "leftJS", 0.0, -1.0, 10.0)]},
    "左右横移": {"what": "左移 10 s → 停 4 s → 右移 10 s → 停 4 s", "space": "机器人左右各留出 4 m 空地",
              "moves": [("左移", "leftJS", -1.0, 0.0, 10.0), ("右移", "leftJS", 1.0, 0.0, 10.0)]},
    "左右转向": {"what": "左转 12 s → 停 4 s → 右转 12 s → 停 4 s（每次约一圈多）", "space": "机器人周围 1 m 内没有人和东西",
              "moves": [("左转", "rightJS", -1.0, 0.0, 12.0), ("右转", "rightJS", 1.0, 0.0, 12.0)]},
}


def timeline(script: str, reps: int) -> tuple[list[tuple[float, float, dict]], float]:
    """[(开始 s, 结束 s, 状态)]，状态 = 按键和摇杆；返回总时长。"""
    held = dict(LG=True, leftGrip=1.0, RG=True, rightGrip=1.0)
    seg = [(0.0, 0.6, {}), (0.6, 1.0, dict(RG=True, rightGrip=1.0, A=True)), (1.0, 3.0, {}), (3.0, LEAD_PUSH, held)]
    t = LEAD_PUSH
    for _ in range(reps):
        for _, stick, x, y, dur in SCRIPTS[script]["moves"]:
            seg.append((t, t + dur, dict(held, **{stick: (x, y)})))
            seg.append((t + dur, t + dur + SETTLE, held))
            t += dur + SETTLE
    seg.append((t, t + B_S, dict(held, B=True)))
    seg.append((t + B_S, t + B_S + 0.5, {}))
    return seg, t + B_S + 0.5


def pushes(script: str, reps: int) -> list[dict]:
    """预计的推杆区间，界面和核对用。"""
    out = []
    for a, b, st in timeline(script, reps)[0]:
        for stick in JS:
            if stick in st:
                kind = next(m[0] for m in SCRIPTS[script]["moves"] if m[1] == stick and (m[2], m[3]) == st[stick])
                out.append({"kind": kind, "start": a, "end": b})
    return out


def _vr(template: bytes, state: dict) -> bytes:
    base = dict(LG=False, leftGrip=0.0, RG=False, rightGrip=0.0, A=False, B=False, X=False, Y=False)
    base.update({k: v for k, v in state.items() if k not in JS})
    d = bytearray(wp.vr_set(template, **base))
    off = wp._body(d)
    for stick, o in JS.items():
        x, y = state.get(stick, (0.0, 0.0))
        struct.pack_into("<ff", d, off + o, x, y)
    return bytes(d)


def make(script: str, reps: int, dst: str | Path, template: Path = TEMPLATE) -> float:
    conns, msgs = be.read_raw(str(template), ["/vr_cmd"])
    first = msgs[0][3]
    (seq0,) = struct.unpack_from("<I", first, 0)
    seg, total = timeline(script, reps)
    out = []
    t0 = 1_000_000_000 * 1000   # 任意起点，播放前会改成当前时间
    period = int(1e9 / HZ)
    i = 0
    for a, b, st in seg:
        frame = _vr(first, st)
        while i * period / 1e9 < b - 1e-9:
            d = bytearray(frame)
            wp.set_header(d, 0, *wp._split(t0 + i * period), seq=seq0 + i)
            out.append((conns[0][0], *wp._split(t0 + i * period), bytes(d)))
            i += 1
    be.write_bag(str(dst), conns, out)
    return total
