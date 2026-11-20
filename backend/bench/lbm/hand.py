"""灵巧手（BrainCo 二代）指令 → 实际：每次指令开合，实际多久跟上；没跟上的次数；开合度误差。只用标准库，主控上也能跑。

/brainco2/hand/cmd：hand_cmd 每只手一条，header.frame_id 写 left / right，pos 6 个（rad）。
/brainco2/hand/state：hand_state 两条，没写左右，按顺序先左后右；约 5–12 Hz。
开合度 = 食指、中指、无名指、小指 4 根的平均（大拇指两轴随手势变，不算）。
"""
from __future__ import annotations

import bisect
import math

CMD = "/brainco2/hand/cmd"
STATE = "/brainco2/hand/state"
HANDS = ("left", "right")
CN = {"left": "左手", "right": "右手"}
FINGERS = slice(2, 6)
RESP_S = 0.5      # 指令开合后，实际过半超过这个时间算慢
MISS_S = 2.0      # 2 s 内没过半算没跟上
MOVE_RAD = 0.2    # 指令开合度最大值低于它：这只手全程没开合


def load(path: str) -> tuple[dict, dict]:
    from . import rosbag_lite as r
    cmd: dict = {h: [] for h in HANDS}
    st: dict = {h: [] for h in HANDS}
    for tp, t, m in r.Bag(path).read([CMD, STATE]):
        if tp == CMD:
            for h in m["hand_cmd"]:
                side = h["header"]["frame_id"]
                if side in cmd and len(h["pos"]) == 6:
                    cmd[side].append((t, sum(h["pos"][FINGERS]) / 4))
        else:
            for side, h in zip(HANDS, m["hand_state"]):
                if len(h["pos"]) == 6:
                    st[side].append((t, sum(h["pos"][FINGERS]) / 4))
    return cmd, st


def _edges(xs: list, th: float) -> list:
    out, prev = [], None
    for t, x in xs:
        cur = x > th
        if prev is not None and cur != prev:
            out.append((t, cur))
        prev = cur
    return out


def follow(cmd: dict, st: dict, a: float, b: float) -> tuple[dict, list]:
    """返回 ({左手/右手: 指标}, [(项, 前级, 后级, 差距, 严重度, 时刻 s)])，时刻相对 a。"""
    res, issues = {}, []
    for side in HANDS:
        C = [x for x in cmd[side] if a <= x[0] <= b]
        S = [x for x in st[side] if a - 1 <= x[0] <= b + MISS_S + 1]
        if len(C) < 20 or len(S) < 5:
            continue
        top = max(x for _, x in C)
        if top < MOVE_RAD:
            res[CN[side]] = {"指令开合次数": 0}
            continue
        th = top / 2
        es = _edges(S, th)
        delays, miss = [], []
        ec = _edges(C, th)
        for t, closed in ec:
            nxt = next((u for u, c in es if c == closed and u >= t - 0.05), None)
            if nxt is None or nxt - t > MISS_S:
                miss.append(round(t - a, 1))
            else:
                delays.append(max(0.0, nxt - t))
        tc = [x[0] for x in C]
        err = [abs(x - C[max(0, bisect.bisect_right(tc, t) - 1)][1]) for t, x in S if tc[0] <= t <= tc[-1]] or [0.0]
        err.sort()
        d = sorted(delays)
        r = {"指令开合次数": len(ec), "实际跟上次数": len(delays), "没跟上时刻s": miss[:10],
             "响应中位s": round(d[len(d) // 2], 2) if d else None, "响应最大s": round(d[-1], 2) if d else None,
             "开合度误差中位deg": round(math.degrees(err[len(err) // 2]), 1), "开合度误差最大deg": round(math.degrees(err[-1]), 1)}
        res[CN[side]] = r
        name = CN[side] + "开合"
        if miss:
            issues.append((name, f"指令 {len(ec)} 次", f"跟上 {len(delays)} 次", f"{len(miss)} 次没跟上", 1 + len(miss), miss[0]))
        if d and d[-1] >= RESP_S:
            issues.append((name, "", "", f"响应最慢 {d[-1]:.2f} s，中位 {r['响应中位s']} s", round(d[-1] / RESP_S, 2), None))
    return res, issues


def hands_bag(path: str) -> dict:
    """整包（跟随段）的灵巧手跟随，给主控上的工作包分析用。"""
    from . import rosbag_lite as r
    st = [(t, (m["data"][1] if len(m["data"]) > 1 else m["data"][0])) for _, t, m in r.Bag(path).read(["/teleop/state"])]
    on = [t for t, v in st if v]
    cmd, hs = load(path)
    if not on:
        return {}
    res, issues = follow(cmd, hs, on[0], on[-1])
    return {"手": res, "差距": [list(i) for i in issues]}
