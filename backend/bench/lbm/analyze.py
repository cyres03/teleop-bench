"""LBMotion 推杆录包分析：每次推杆按链路逐级算“发了多少、实际走了多少”，以及松杆后多走了多少。

链路（录包里逐级都有，统一用录包接收时间）：
  /cmd_vel（仲裁后的速度指令） → /lbmotion_states（LBMotion 内部指令：data[1] 前后速度、data[2] 转向角速度、
  data[3] 行走标志；横移没有对应字段） → 实际（骨盆位移：腿部正运动学 + IMU 姿态、支撑脚不动积分，同 leg_odom.py；
  朝向：骨盆 IMU 偏航）。

每次推杆（/cmd_vel 非零、主方向不变、帧间隔 < 0.3 s 的连续段）给出：
  稳态：推杆开始 LEAD_S 后到松杆的区间里，指令均值、LBMotion 内部均值、实际速度，实际 / 指令；
  推杆全程：主方向行程、侧向偏移、偏移率（侧向 / 行程）、朝向变化；
  松杆后：身体停下时间（骨盆水平速度连续 0.5 s < 0.05 m/s；转向看偏航角速度连续 0.5 s < 5°/s）、
  主方向多走的距离或多转的角度、朝向变化、抬脚次数。
"""
from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import dataclass, field

CMD_ZERO = 0.05
GAP_S = 0.3
LEAD_S = 2.0
MIN_STEADY_S = 1.0
BODY_V, BODY_HOLD = 0.05, 0.5
YAW_STILL_DEG_S = 5.0
LIFT = 0.3
POST_S = 15.0
WIN = 0.25

KINDS = {
    ("x", 1): "前进", ("x", -1): "后退",
    ("y", 1): "左移", ("y", -1): "右移",
    ("z", 1): "左转", ("z", -1): "右转",
}


@dataclass
class Series:
    """全部按录包接收时间（秒）排序。"""
    cmd: list = field(default_factory=list)    # (t, vx, vy, wz)
    lbm: list = field(default_factory=list)    # (t, vx_int, wz_int)
    yaw: list = field(default_factory=list)    # (t, yaw_rad 连续展开)
    pos: list = field(default_factory=list)    # (t, x, y) 世界系骨盆水平位置
    knee: list = field(default_factory=list)   # (t, 左膝, 右膝)
    lean: list = field(default_factory=list)   # (t, 骨盆前倾°, 胸腔前倾°)；前倾为正，胸腔无腰关节数据时为 None


def _mm(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def _rot(axis, a):
    c, s_ = math.cos(a), math.sin(a)
    if axis == "x":
        return ((1, 0, 0), (0, c, -s_), (0, s_, c))
    if axis == "y":
        return ((c, 0, s_), (0, 1, 0), (-s_, 0, c))
    return ((c, -s_, 0), (s_, c, 0), (0, 0, 1))


def forward_lean_deg(r) -> float:
    """机身 x 轴（朝前）低于水平面的角度，前倾为正；与欧拉角顺序、符号约定无关。"""
    return math.degrees(-math.asin(max(-1.0, min(1.0, r[2][0]))))


# ---------------- 读录包 ----------------
def load_bag(path: str, step: int = 4) -> Series:
    from . import leg_odom as lo
    from . import rosbag_lite as r

    names = lo.LEGS["L"][0] + lo.LEGS["R"][0]
    waist_names = ("waist_yaw_joint", "waist_roll_joint", "waist_pitch_joint")
    imu, js, s, idx, widx = [], [], Series(), None, None
    for tp, t, m in r.Bag(path).read(["/ImuData", "/joint/state", "/cmd_vel", "/lbmotion_states"]):
        if tp == "/ImuData":
            imu.append((m["header"]["stamp"], t, m["quat"]))
        elif tp == "/joint/state":
            if idx is None:
                idx = [m["names"].index(n) for n in names]
                widx = [m["names"].index(n) for n in waist_names] if all(n in m["names"] for n in waist_names) else []
            js.append((m["header"]["stamp"], t, [m["q"][i] for i in idx] + [m["q"][i] for i in widx]))
        elif tp == "/cmd_vel":
            s.cmd.append((t, m["linear"]["x"], m["linear"]["y"], m["angular"]["z"]))
        else:
            d = m["data"]
            if len(d) > 2:
                s.lbm.append((t, d[1], d[2]))
    if not s.cmd or not js or not imu:
        raise ValueError("录包缺 /cmd_vel、/joint/state 或 /ImuData")
    # 各话题 header.stamp 不是同一时钟（659 差约 1.9 s），按“接收时间 − stamp”的最小值换到录包时钟
    oi = min(x[1] - x[0] for x in imu)
    oj = min(x[1] - x[0] for x in js)
    # 录包积压时接收时间会比真实时间晚（11-11 一份 1.3 GB 全录晚 0.5–0.8 s）；没有 header 的
    # /cmd_vel、/lbmotion_states 按同一时刻 IMU 的积压量往前校正
    lag = sorted((x[1], x[1] - x[0] - oi) for x in imu)
    lt = [x[0] for x in lag]

    def fix(t):
        i = min(bisect.bisect_left(lt, t), len(lag) - 1)
        return t - lag[i][1]

    s.cmd = [(fix(c[0]),) + tuple(c[1:]) for c in s.cmd]
    s.lbm = [(fix(b[0]),) + tuple(b[1:]) for b in s.lbm]
    imu = sorted((x[0] + oi, x[2]) for x in imu)
    it = [x[0] for x in imu]
    jsa = sorted(((x[0] + oj, x[2]) for x in js), key=lambda x: x[0])

    prev_yaw, off = None, 0.0
    for t, (w, x, y, z) in imu[::5]:
        a = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
        if prev_yaw is not None:
            d = a - prev_yaw
            off -= 2 * math.pi * round(d / (2 * math.pi))
        prev_yaw = a
        s.yaw.append((t, a + off))

    px = py = 0.0
    stance = prev = None
    for t, q in jsa[::step]:
        if not it[0] <= t <= it[-1]:
            continue
        rw = lo.quat_mat(*imu[min(bisect.bisect_left(it, t), len(imu) - 1)][1])
        fw = {"L": lo.mv(rw, lo.foot("L", q[:6])), "R": lo.mv(rw, lo.foot("R", q[6:12]))}
        low = "L" if fw["L"][2] < fw["R"][2] else "R"
        if stance is None or (low != stance and abs(fw["L"][2] - fw["R"][2]) > 0.01):
            stance, prev = low, fw[low]
        else:
            f = fw[stance]
            px -= f[0] - prev[0]
            py -= f[1] - prev[1]
            prev = f
        s.pos.append((t, px, py))
        s.knee.append((t, q[3], q[9]))
        chest = None
        if len(q) >= 15:
            chest = forward_lean_deg(_mm(_mm(_mm(rw, _rot("z", q[12])), _rot("x", q[13])), _rot("y", q[14])))
        s.lean.append((t, forward_lean_deg(rw), chest))
    s.cmd.sort()
    s.lbm.sort()
    return s


# ---------------- 分析 ----------------
def _axis(c) -> tuple[str, int] | None:
    vx, vy, wz = c[1], c[2], c[3]
    if max(abs(vx), abs(vy), abs(wz)) <= CMD_ZERO:
        return None
    if abs(vx) <= CMD_ZERO and abs(vy) <= CMD_ZERO:
        return ("z", 1 if wz > 0 else -1)
    if abs(vx) >= abs(vy):
        return ("x", 1 if vx > 0 else -1)
    return ("y", 1 if vy > 0 else -1)


def _segments(cmd):
    segs, cur = [], None
    for i, c in enumerate(cmd):
        ax = _axis(c)
        if ax is None:
            cur = None
            continue
        if cur and cur["ax"] == ax and c[0] - cmd[cur["i1"]][0] < GAP_S:
            cur["i1"] = i
        else:
            cur = {"i0": i, "i1": i, "ax": ax}
            segs.append(cur)
    return segs


class _Idx:
    def __init__(self, data):
        self.d = data
        self.t = [x[0] for x in data]

    def at(self, t):
        if not self.d:
            return None
        return self.d[min(max(bisect.bisect_left(self.t, t), 0), len(self.d) - 1)]

    def between(self, a, b):
        return self.d[bisect.bisect_left(self.t, a):bisect.bisect_right(self.t, b)]


def _mean(xs):
    return statistics.fmean(xs) if xs else None


def _r(x, n=3):
    return None if x is None else round(x, n)


def analyze(s: Series, min_push: float = 0.5, post: float = POST_S) -> list[dict]:
    cmd = s.cmd
    pos, yaw, lbm, knee, lean = _Idx(s.pos), _Idx(s.yaw), _Idx(s.lbm), _Idx(s.knee), _Idx(s.lean)
    if not pos.d or not yaw.d:
        return []
    t0 = cmd[0][0]
    end_t = min(pos.t[-1], yaw.t[-1])
    start_t = max(pos.t[0], yaw.t[0])
    segs = _segments(cmd)
    out = []
    for n, g in enumerate(segs):
        ts = cmd[g["i0"]][0]
        released = g["i1"] + 1 < len(cmd) and _axis(cmd[g["i1"] + 1]) is None
        tc = cmd[g["i1"] + 1][0] if g["i1"] + 1 < len(cmd) else cmd[g["i1"]][0]
        push = tc - ts
        if push < min_push or ts > end_t:
            continue
        ax, sign = g["ax"]
        kind = KINDS[(ax, sign)]
        k = {"x": 1, "y": 2, "z": 3}[ax]
        h = yaw.at(ts)[1]
        ch, sh = math.cos(h), math.sin(h)

        def proj(p0, p1):
            dx, dy = p1[1] - p0[1], p1[2] - p0[2]
            return dx * ch + dy * sh, -dx * sh + dy * ch

        # 稳态：推杆开始 LEAD_S 后到松杆；推杆太短时取后一半
        a = ts + min(LEAD_S, push / 2)
        steady = tc - a >= MIN_STEADY_S
        cmd_main = _mean([c[k] for c in cmd[g["i0"]:g["i1"] + 1] if a <= c[0] <= tc])
        if cmd_main is None:
            cmd_main = _mean([c[k] for c in cmd[g["i0"]:g["i1"] + 1]])
        lbm_main = None
        if ax in ("x", "z") and lbm.d:
            vals = [x[1] if ax == "x" else x[2] for x in lbm.between(a, tc) if x[0] < tc]
            lbm_main = _mean(vals)
        if ax == "z":
            act = (yaw.at(tc)[1] - yaw.at(a)[1]) / max(tc - a, 1e-3)
            unit = "°/s"
            cmd_v, lbm_v, act_v = math.degrees(cmd_main), None if lbm_main is None else math.degrees(lbm_main), math.degrees(act)
        else:
            f, l = proj(pos.at(a), pos.at(tc))
            act = (f if ax == "x" else l) / max(tc - a, 1e-3)
            unit = "m/s"
            cmd_v, lbm_v, act_v = cmd_main, lbm_main, act

        # 推杆全程
        f_all, l_all = proj(pos.at(ts), pos.at(tc))
        yaw_push = math.degrees(yaw.at(tc)[1] - yaw.at(ts)[1])
        travel = f_all if ax == "x" else l_all if ax == "y" else None
        side = l_all if ax == "x" else f_all if ax == "y" else None

        # 松杆后
        tn = cmd[segs[n + 1]["i0"]][0] if n + 1 < len(segs) else math.inf
        te = min(tc + post, tn, end_t)
        stop = None
        if released:
            if ax == "z":
                quiet = None
                for t, yv in yaw.between(tc, te):
                    p = yaw.at(t - WIN)
                    rate = abs(math.degrees(yv - p[1])) / max(t - p[0], 1e-3)
                    if rate < YAW_STILL_DEG_S:
                        quiet = t if quiet is None else quiet
                        if t - quiet >= BODY_HOLD:
                            stop = quiet
                            break
                    else:
                        quiet = None
            else:
                quiet, last = None, pos.at(tc)
                for p in pos.between(tc, te):
                    if p[0] - last[0] >= 0.1:
                        v = math.hypot(p[1] - last[1], p[2] - last[2]) / (p[0] - last[0])
                        last = p
                        if v < BODY_V:
                            quiet = p[0] if quiet is None else quiet
                            if p[0] - quiet >= BODY_HOLD:
                                stop = quiet
                                break
                        else:
                            quiet = None
        tend = stop if stop is not None else te
        cf, cl = proj(pos.at(tc), pos.at(tend))
        coast_yaw = math.degrees(yaw.at(tend)[1] - yaw.at(tc)[1])
        coast_main = coast_yaw if ax == "z" else cf if ax == "x" else cl

        pre = [x for x in knee.between(ts - 1.0, ts)] or knee.between(ts, tc)
        steps = 0
        if pre and released:
            bl = sorted(x[1] for x in pre)[len(pre) // 2]
            br = sorted(x[2] for x in pre)[len(pre) // 2]
            up = {"L": False, "R": False}
            for t, lk, rk in knee.between(tc - 0.5, tend):
                for leg, fl in (("L", lk - bl), ("R", rk - br)):
                    if not up[leg] and fl > LIFT:
                        up[leg] = True
                        steps += t >= tc
                    elif up[leg] and fl < LIFT * 0.5:
                        up[leg] = False

        lean_m = {"lean_base_ok": not any(_axis(c) for c in cmd if ts - 1.0 <= c[0] < ts) and ts - start_t >= 1.0}
        for j, part in ((1, "pelvis"), (2, "chest")):
            # 基线只用推杆前 1 s 完全没有指令的站立段，否则不出前倾数据
            base = [x[j] for x in lean.between(ts - 1.0, ts) if x[j] is not None] if lean_m["lean_base_ok"] else []
            b = statistics.median(base) if base else None
            st = [x[j] for x in lean.between(a, tc) if x[j] is not None]
            during = [x[j] for x in lean.between(ts, tc) if x[j] is not None]
            after = [x[j] for x in lean.between(tc, min(tc + 2.0, te)) if x[j] is not None]
            lean_m[f"lean_{part}_base"] = _r(b, 2)
            lean_m[f"lean_{part}_steady"] = _r(_mean(st) - b, 2) if b is not None and st else None
            lean_m[f"lean_{part}_max"] = _r(max(during) - b, 2) if b is not None and during else None
            lean_m[f"lean_{part}_after"] = _r(max(after) - b, 2) if b is not None and after else None

        out.append({
            **lean_m,
            "kind": kind, "axis": ax, "unit": unit,
            "t": _r(ts - t0, 2), "push_s": _r(push, 2), "steady_s": _r(tc - a, 2), "steady": steady,
            "cmd": _r(cmd_v), "lbm": _r(lbm_v), "actual": _r(act_v),
            "lbm_over_cmd": _r(lbm_v / cmd_v, 3) if lbm_v is not None and cmd_v else None,
            "actual_over_lbm": _r(act_v / lbm_v, 3) if lbm_v else None,
            "actual_over_cmd": _r(act_v / cmd_v, 3) if cmd_v else None,
            "travel_m": _r(travel), "side_m": _r(side),
            "side_rate": _r(side / abs(travel), 4) if travel and abs(travel) > 0.2 else None,
            "yaw_push_deg": _r(yaw_push, 1),
            "cut_start": ts - start_t < 0.5, "released": released, "interrupted": released and stop is None and te == tn,
            "stop_s": None if stop is None else _r(stop - tc, 2),
            "window_s": _r(te - tc, 2),
            "coast_main": _r(coast_main, 3 if ax != "z" else 1), "coast_yaw_deg": _r(coast_yaw, 1),
            "coast_steps": steps,
        })
    return out


def analyze_bag(path: str, **kw) -> list[dict]:
    return analyze(load_bag(path), **kw)


# ---------------- 汇总 ----------------
METRICS = [
    ("cmd", "指令"), ("lbm", "LBMotion 内部"), ("actual", "实际"),
    ("lbm_over_cmd", "内部/指令"), ("actual_over_lbm", "实际/内部"), ("actual_over_cmd", "实际/指令"),
    ("travel_m", "推杆行程 m"), ("side_m", "侧向偏移 m"), ("side_rate", "偏移率"), ("yaw_push_deg", "推杆中朝向变化 °"),
    ("lean_pelvis_steady", "骨盆前倾（稳态均值）°"), ("lean_pelvis_max", "骨盆前倾（推杆最大）°"),
    ("lean_chest_steady", "胸腔前倾（稳态均值）°"), ("lean_chest_max", "胸腔前倾（推杆最大）°"),
    ("stop_s", "松杆到停下 s"), ("coast_main", "松杆后多走"), ("coast_yaw_deg", "松杆后朝向变化 °"), ("coast_steps", "松杆后抬脚次数"),
]


def summarize(segments: list[dict]) -> list[dict]:
    rows = []
    for kind in dict.fromkeys(s["kind"] for s in segments):
        seg = [s for s in segments if s["kind"] == kind]
        seg = [s for s in seg if not s["cut_start"]] or seg
        good = [s for s in seg if s["steady"]]
        row = {"kind": kind, "unit": seg[0]["unit"], "n": len(seg), "n_steady": len(good),
               "n_not_stopped": sum(1 for s in seg if s["released"] and s["stop_s"] is None)}
        for key, _ in METRICS:
            src = good if key in ("cmd", "lbm", "actual", "lbm_over_cmd", "actual_over_lbm", "actual_over_cmd") else seg
            vals = [s[key] for s in src if s[key] is not None]
            row[key] = None if not vals else {"median": round(statistics.median(vals), 4), "min": min(vals), "max": max(vals), "n": len(vals)}
        rows.append(row)
    return rows
