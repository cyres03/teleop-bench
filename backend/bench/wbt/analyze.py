"""全身遥操（WBT）跟随对比，分三级：人 → 重定向 → 机器人。

- 人：Pico 全身动捕 /tf（pico_origin → /vr_body/*），链式累加到世界系。
- 重定向：/teleop/wb_cmd 里 frame_id=world 的消息，anchors（各部位世界位姿）+ joint_cmd（31 个关节指令）。
- 机器人：/ImuData 偏航、/joint/state 关节实测、腿部正运动学里程计（660 的 /tf base_link 只有旋转，没有位移）。
只看跟随段（/teleop/state data[1] = 1）。时间一律用录包接收时间，回放包里的旧 stamp 不影响。
"""
from __future__ import annotations

import bisect
import math
from pathlib import Path

import numpy as np

from ..lbm import hand as H
from ..lbm import leg_odom as L
from ..lbm import rosbag_lite as rl

TOPICS = ["/tf", "/teleop/wb_cmd", "/teleop/state", "/teleop/session_state", "/ImuData", "/joint/state",
          "/brainco2/hand/cmd", "/brainco2/hand/state"]
HANDS = ("left", "right")
HAND_CN = {"left": "左手", "right": "右手"}
FINGERS = slice(2, 6)  # 食指、中指、无名指、小指；开合度取这四根的平均（rad），同 lbm/hand.py
VB = "/vr_body/"
SPINE = ["Pelvis", "SPINE1", "SPINE2", "SPINE3"]
# 部位：人的骨骼链（从骨盆起）↔ 重定向锚点
PARTS = {
    "头": (SPINE + ["NECK", "HEAD"], "head_pitch_link"),
    "左手": (SPINE + ["LEFT_COLLAR", "LEFT_SHOULDER", "LEFT_ELBOW", "LEFT_WRIST"], "left_wrist_roll_link"),
    "右手": (SPINE + ["RIGHT_COLLAR", "RIGHT_SHOULDER", "RIGHT_ELBOW", "RIGHT_WRIST"], "right_wrist_roll_link"),
    "左脚": (["Pelvis", "LEFT_HIP", "LEFT_KNEE", "LEFT_ANKLE"], "left_ankle_roll_link"),
    "右脚": (["Pelvis", "RIGHT_HIP", "RIGHT_KNEE", "RIGHT_ANKLE"], "right_ankle_roll_link"),
}
AXES = ("前后", "左右", "上下")
GRID = 0.02          # 对比统一到 50 Hz
MAX_LAG = 1.0        # 滞后搜索范围，s
# 列入“差距清单”的门限
TH = {
    "位移差m": 0.2,          # 前进或侧移净位移差
    "位移比": (0.8, 1.2),    # 前级位移 ≥ 0.5 m 时，后级/前级
    "偏航净差deg": 10.0,
    "转动比": (0.9, 1.1),    # 先转、回转幅度，前级 ≥ 90° 时，后级/前级
    "偏航曲线差deg": 15.0,
    "轨迹差m": 0.3,
    "幅度比": (0.8, 1.2),    # 部位活动幅度，前级幅度 ≥ 0.1 m 才算
    "相关": 0.9,
    "滞后s": 0.15,
    "关节均方根deg": 5.0,
}


# ---------- 几何 ----------
def qmul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (aw * bw - ax * bx - ay * by - az * bz, aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx, aw * bz + ax * by - ay * bx + az * bw)


def qrot(q, v):
    w, x, y, z = q
    r = qmul(qmul(q, (0.0, *v)), (w, -x, -y, -z))
    return r[1:]


def yaw_of(q):
    w, x, y, z = q
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


def _q(o):
    return (o["w"], o["x"], o["y"], o["z"])


def _v(p):
    return (p["x"], p["y"], p["z"])


def to_body(d, yaw):
    """世界系向量转到“只按偏航转正”的身体系：x 前、y 左、z 上。"""
    c, s = math.cos(-yaw), math.sin(-yaw)
    return (d[0] * c - d[1] * s, d[0] * s + d[1] * c, d[2])


# ---------- 读包 ----------
def load(path: str) -> dict:
    tf: dict[str, tuple] = {}
    hum, ret, st, imu, js, modes = [], [], [], [], [], []
    hcmd: dict[str, list] = {h: [] for h in HANDS}
    hst: dict[str, list] = {h: [] for h in HANDS}
    need = {VB + f for ch, _ in PARTS.values() for f in ch}
    jnames = None
    for tp, t, m in rl.Bag(path).read(TOPICS):
        if tp == "/tf":
            upd = False
            for tr in m["transforms"]:
                c = tr["child_frame_id"]
                if c.startswith(VB):
                    tf[c] = (_v(tr["transform"]["translation"]), _q(tr["transform"]["rotation"]))
                    upd = upd or c == VB + "Pelvis"
            if upd and need <= tf.keys():
                pts = {}
                for k, (ch, _) in PARTS.items():
                    p, q = (0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0)
                    for f in ch:
                        tp_, tq = tf[VB + f]
                        p = tuple(a + b for a, b in zip(p, qrot(q, tp_)))
                        q = qmul(q, tq)
                    pts[k] = p
                pp, pq = tf[VB + "Pelvis"]
                hum.append((t, pp, yaw_of(pq), pts))
        elif tp == "/teleop/wb_cmd":
            if m["header"]["frame_id"] != "world":
                continue
            A = {a["name"]: (_v(a["pose"]["position"]), _q(a["pose"]["orientation"])) for a in m["anchors"]}
            jc = m["joint_cmd"]
            ret.append((t, A, dict(zip(jc["names"], jc["q"]))))
        elif tp == "/teleop/state":
            d = m["data"]
            st.append((t, d[1] if len(d) > 1 else d[0]))
        elif tp == "/teleop/session_state":
            modes.append((t, m.get("mode", "")))
        elif tp == "/ImuData":
            imu.append((t, tuple(m["quat"]), m["euler"][2], m["euler"][0], m["euler"][1]))
        elif tp == "/joint/state":
            if jnames is None:
                jnames = list(m["names"])
            js.append((t, list(m["q"])))
        elif tp == "/brainco2/hand/cmd":
            for h in m["hand_cmd"]:
                side = h["header"]["frame_id"]
                if side in hcmd and len(h["pos"]) == 6:
                    hcmd[side].append((t, float(np.mean(h["pos"][FINGERS]))))
        elif tp == "/brainco2/hand/state":
            for side, h in zip(HANDS, m["hand_state"]):  # 状态里没写左右，按顺序：先左后右
                if len(h["pos"]) == 6:
                    hst[side].append((t, float(np.mean(h["pos"][FINGERS]))))
    return {"hum": hum, "ret": ret, "st": st, "imu": imu, "js": js, "jnames": jnames or [], "modes": modes,
            "hcmd": hcmd, "hst": hst}


def hands(raw: dict, a: float, b: float) -> tuple[dict, list, dict]:
    """灵巧手 指令 → 实际（算法在 lbm/hand.py，和 VR部分动捕工作包共用）；另给画图用的曲线。"""
    res, raw_issues = H.follow(raw["hcmd"], raw["hst"], a, b)
    issues = [_iss("重定向→机器人", *i) for i in raw_issues]
    series = {}
    for side in HANDS:
        C = [x for x in raw["hcmd"][side] if a <= x[0] <= b]
        S = [x for x in raw["hst"][side] if a <= x[0] <= b]
        if HAND_CN[side] in res and res[HAND_CN[side]].get("指令开合次数") and C and S:
            series[side] = (np.array([x[0] for x in C]) - a, np.degrees([x[1] for x in C]),
                            np.array([x[0] for x in S]) - a, np.degrees([x[1] for x in S]))
    return res, issues, series


def posture(raw: dict, a: float, b: float, t_rel) -> dict:
    """骨盆最大俯仰、横滚（IMU），回放前判断危险用。"""
    imu = [x for x in raw["imu"] if a <= x[0] <= b]
    if not imu:
        return {}
    r = np.degrees([x[3] for x in imu])
    p = np.degrees([x[4] for x in imu])
    kr, kp = int(np.argmax(np.abs(r))), int(np.argmax(np.abs(p)))
    return {"俯仰最大deg": round(float(p[kp]), 1), "俯仰时刻s": t_rel(imu[kp][0]),
            "横滚最大deg": round(float(r[kr]), 1), "横滚时刻s": t_rel(imu[kr][0])}


def follow_window(st: list) -> tuple[float, float] | None:
    """最长的一段连续跟随。"""
    best, a = None, None
    for t, v in st + [(math.inf, 0)]:
        if v and a is None:
            a = t
        elif not v and a is not None:
            end = t if t != math.inf else st[-1][0]
            if best is None or end - a > best[1] - best[0]:
                best = (a, end)
            a = None
    return best


# ---------- 统计 ----------
def unwrap(a):
    return np.unwrap(np.asarray(a, float)) if len(a) else np.zeros(0)


def rng(x) -> float:
    x = np.asarray(x, float)
    return float(np.percentile(x, 98) - np.percentile(x, 2)) if len(x) else 0.0


def on_grid(t, x, g):
    return np.interp(g, np.asarray(t, float), np.asarray(x, float))


def lag_corr(ta, a, tb, b) -> tuple[float, float]:
    """b 相对 a 的滞后（s，正 = b 晚）和该滞后下的相关系数。"""
    t0, t1 = max(ta[0], tb[0]), min(ta[-1], tb[-1])
    if t1 - t0 < 2 * MAX_LAG:
        return float("nan"), float("nan")
    g = np.arange(t0, t1, GRID)
    A, B = on_grid(ta, a, g), on_grid(tb, b, g)
    A, B = A - A.mean(), B - B.mean()
    if A.std() < 1e-9 or B.std() < 1e-9:
        return float("nan"), float("nan")
    best = (-2.0, 0)
    n = int(MAX_LAG / GRID)
    for k in range(-n, n + 1):
        x, y = (A[: len(A) - k], B[k:]) if k >= 0 else (A[-k:], B[: len(B) + k])
        c = float(np.corrcoef(x, y)[0, 1])
        if c > best[0]:
            best = (c, k)
    return round(best[1] * GRID, 2), round(best[0], 3)


def net_disp(P, yaw0):
    """净位移，按起始朝向分成前进（+前）和侧移（+左）。"""
    d = to_body((P[-1][0] - P[0][0], P[-1][1] - P[0][1], 0.0), yaw0)
    return round(d[0], 2), round(d[1], 2)


def zeroed_xy(P, yaw0):
    return np.array([to_body((p[0] - P[0][0], p[1] - P[0][1], 0.0), yaw0)[:2] for p in P])


def leg_odometry(imu, js, jnames, a, b):
    """骨盆水平位移（世界系，起点 0）：支撑脚不动，正运动学积分。"""
    idx = {s: [jnames.index(n) for n in L.LEGS[s][0]] for s in "LR"}
    it = [x[0] for x in imu]
    px = py = 0.0
    stance = prev = None
    out = []
    for t, q in js[::4]:
        if not a <= t <= b:
            continue
        Rw = L.quat_mat(*imu[min(bisect.bisect_left(it, t), len(imu) - 1)][1])
        fw = {s: L.mv(Rw, L.foot(s, [q[i] for i in idx[s]])) for s in "LR"}
        low = "L" if fw["L"][2] < fw["R"][2] else "R"
        if stance is None or (low != stance and abs(fw["L"][2] - fw["R"][2]) > 0.01):
            stance, prev = low, fw[low]
        else:
            f = fw[stance]
            px -= f[0] - prev[0]
            py -= f[1] - prev[1]
            prev = f
        out.append((t, px, py))
    return out


# ---------- 主分析 ----------
def analyze_bag(path: str) -> dict:
    raw = load(path)
    if not raw["st"]:
        raise ValueError("录包里没有 /teleop/state，判断不了跟随区间")
    win = follow_window(raw["st"])
    if not win:
        raise ValueError("录包里没有进入跟随（/teleop/state 一直是 0）")
    a, b = win
    H = [x for x in raw["hum"] if a <= x[0] <= b]
    W = [x for x in raw["ret"] if a <= x[0] <= b]
    if len(H) < 50:
        raise ValueError("跟随段里没有人体动捕数据（/tf /vr_body/*）：确认用的是 Pico 全身动捕")
    if len(W) < 50:
        raise ValueError("跟随段里没有重定向输出（/teleop/wb_cmd world）")
    th = np.array([x[0] for x in H])
    tw = np.array([x[0] for x in W])
    hy = unwrap([x[2] for x in H])
    ry = unwrap([yaw_of(x[1]["base_link"][1]) for x in W])
    hP = [x[1] for x in H]
    rP = [x[1]["base_link"][0] for x in W]
    ms = [m for t, m in raw["modes"] if a <= t <= b and m]
    res: dict = {"bag": Path(path).name, "follow_s": round(b - a, 1), "t0": round(a, 3),
                 "控制器": max(set(ms), key=ms.count) if ms else ""}
    issues: list[dict] = []

    def t_rel(t):  # 跟随开始后第几秒
        return round(float(t) - a, 1)

    # --- 1. 人 → 重定向 ---
    s1: dict = {}
    hd, rd = net_disp(hP, hy[0]), net_disp(rP, ry[0])
    s1["位移"] = {"人": {"前进": hd[0], "侧移": hd[1]}, "重定向": {"前进": rd[0], "侧移": rd[1]}}
    hyz, ryz = np.degrees(hy - hy[0]), np.degrees(ry - ry[0])
    g = np.arange(max(th[0], tw[0]), min(th[-1], tw[-1]), GRID)
    ydiff = on_grid(tw, ryz, g) - on_grid(th, hyz, g)
    k = int(np.argmax(np.abs(ydiff)))
    s1["偏航"] = {"人净": round(float(hyz[-1]), 1), "重定向净": round(float(ryz[-1]), 1),
                  "人转动": turns(hyz), "重定向转动": turns(ryz),
                  "曲线最大差": round(float(ydiff[k]), 1), "最大差时刻s": t_rel(g[k])}
    s1["偏航"]["滞后s"], s1["偏航"]["相关"] = lag_corr(th, hy, tw, ry)
    hn, rn = zeroed_xy(hP, hy[0]), zeroed_xy(rP, ry[0])
    e = np.hypot(on_grid(tw, rn[:, 0], g) - on_grid(th, hn[:, 0], g), on_grid(tw, rn[:, 1], g) - on_grid(th, hn[:, 1], g))
    k = int(np.argmax(e))
    s1["轨迹差"] = {"均方根m": round(float(np.sqrt((e ** 2).mean())), 2), "最大m": round(float(e[k]), 2), "最大差时刻s": t_rel(g[k])}
    hz, rz = [p[2] for p in hP], [p[2] for p in rP]
    s1["骨盆高度"] = {"人幅度m": round(rng(hz), 3), "重定向幅度m": round(rng(rz), 3)}
    parts, rel = {}, {}
    for name, (_, anchor) in PARTS.items():
        hrel = np.array([to_body(tuple(p - q for p, q in zip(x[3][name], x[1])), y) for x, y in zip(H, hy)])
        rrel = np.array([to_body(tuple(p - q for p, q in zip(x[1][anchor][0], x[1]["base_link"][0])), y) for x, y in zip(W, ry)])
        rel[name] = (hrel, rrel)
        for i, ax in enumerate(AXES):
            ra, rb = rng(hrel[:, i]), rng(rrel[:, i])
            lg, c = lag_corr(th, hrel[:, i], tw, rrel[:, i])
            parts[f"{name}{ax}"] = {"人幅度m": round(ra, 3), "重定向幅度m": round(rb, 3),
                                   "比": round(rb / ra, 2) if ra >= 0.1 else None, "相关": c, "滞后s": lg}
    s1["部位"] = parts
    res["人→重定向"] = s1
    issues += _disp_issues("人→重定向", hd, rd)
    issues += _yaw_issues("人→重定向", s1["偏航"]["人净"], s1["偏航"]["重定向净"], s1["偏航"]["曲线最大差"], s1["偏航"]["最大差时刻s"])
    issues += _turn_issues("人→重定向", s1["偏航"]["人转动"], s1["偏航"]["重定向转动"])
    if s1["轨迹差"]["最大m"] >= TH["轨迹差m"]:
        issues.append(_iss("人→重定向", "骨盆轨迹", "", "", f"最大偏离 {s1['轨迹差']['最大m']} m",
                           s1["轨迹差"]["最大m"] / TH["轨迹差m"], s1["轨迹差"]["最大差时刻s"]))
    for key, v in parts.items():
        if v["比"] is None:
            continue
        lo, hi = TH["幅度比"]
        if not lo <= v["比"] <= hi:
            issues.append(_iss("人→重定向", key, f"{v['人幅度m']} m", f"{v['重定向幅度m']} m", f"幅度比 {v['比']}",
                               abs(1 - v["比"]) / (1 - lo)))
        if v["相关"] == v["相关"] and v["相关"] < TH["相关"]:
            issues.append(_iss("人→重定向", key, "", "", f"相关 {v['相关']}", (1 - v["相关"]) / (1 - TH["相关"])))
        if v["滞后s"] == v["滞后s"] and v["滞后s"] >= TH["滞后s"]:
            issues.append(_iss("人→重定向", key, "", "", f"滞后 {v['滞后s']} s", v["滞后s"] / TH["滞后s"]))

    # --- 2. 重定向 → 机器人 ---
    s2: dict = {}
    series = {"th": th - a, "tw": tw - a, "hyz": hyz, "ryz": ryz, "hn": hn, "rn": rn, "rel": rel}
    imu = [x for x in raw["imu"] if a <= x[0] <= b]
    if len(imu) > 50:
        ti = np.array([x[0] for x in imu])
        iy = unwrap([x[2] for x in imu])
        iyz = np.degrees(iy - iy[0])
        g2 = np.arange(max(tw[0], ti[0]), min(tw[-1], ti[-1]), GRID)
        d2 = on_grid(ti, iyz, g2) - on_grid(tw, ryz, g2)
        k = int(np.argmax(np.abs(d2)))
        s2["偏航"] = {"重定向净": round(float(ryz[-1]), 1), "机器人净": round(float(iyz[-1]), 1),
                      "重定向转动": turns(ryz), "机器人转动": turns(iyz),
                      "曲线最大差": round(float(d2[k]), 1), "最大差时刻s": t_rel(g2[k])}
        s2["偏航"]["滞后s"], s2["偏航"]["相关"] = lag_corr(tw, ry, ti, iy)
        issues += _yaw_issues("重定向→机器人", s2["偏航"]["重定向净"], s2["偏航"]["机器人净"], s2["偏航"]["曲线最大差"],
                              s2["偏航"]["最大差时刻s"])
        issues += _turn_issues("重定向→机器人", s2["偏航"]["重定向转动"], s2["偏航"]["机器人转动"])
        series["ti"], series["iyz"] = ti - a, iyz
        if raw["js"] and all(n in raw["jnames"] for s in "LR" for n in L.LEGS[s][0]):
            od = leg_odometry(raw["imu"], raw["js"], raw["jnames"], a, b)
            if len(od) > 10:
                oP = [(x[1], x[2], 0.0) for x in od]
                odd = net_disp(oP, float(iy[0]))
                s2["位移"] = {"重定向": {"前进": rd[0], "侧移": rd[1]}, "机器人": {"前进": odd[0], "侧移": odd[1]}}
                issues += _disp_issues("重定向→机器人", rd, odd)
                series["on"] = zeroed_xy(oP, float(iy[0]))
    jn = raw["jnames"]
    js = [x for x in raw["js"] if a <= x[0] <= b]
    if js and jn:
        tj = np.array([x[0] for x in js])
        Q = np.array([x[1] for x in js])
        rows, curves = [], {}
        for n in W[0][2]:
            if n not in jn:
                continue
            cmd = np.array([x[2][n] for x in W])
            act = Q[:, jn.index(n)]
            g3 = np.arange(max(tw[0], tj[0]), min(tw[-1], tj[-1]), GRID)
            d3 = np.degrees(on_grid(tj, act, g3) - on_grid(tw, cmd, g3))
            k = int(np.argmax(np.abs(d3)))
            lg, c = lag_corr(tw, cmd, tj, act)
            rows.append({"关节": n, "均方根deg": round(float(np.sqrt((d3 ** 2).mean())), 1), "最大deg": round(float(d3[k]), 1),
                         "最大差时刻s": t_rel(g3[k]), "指令幅度deg": round(math.degrees(rng(cmd)), 1), "滞后s": lg, "相关": c})
            curves[n] = (np.degrees(cmd), np.degrees(act))
        rows.sort(key=lambda r: -r["均方根deg"])
        s2["关节"] = rows
        series["tj"], series["joints"] = tj - a, curves
        for r in rows:
            if r["均方根deg"] >= TH["关节均方根deg"]:
                issues.append(_iss("重定向→机器人", r["关节"], f"指令幅度 {r['指令幅度deg']}°", "",
                                   f"跟踪误差均方根 {r['均方根deg']}°，最大 {r['最大deg']:+}°", r["均方根deg"] / TH["关节均方根deg"],
                                   r["最大差时刻s"]))
    hs, hi, hser = hands(raw, a, b)
    if hs:
        s2["灵巧手"] = hs
        issues += hi
        series["hands"] = hser
    s2["姿态"] = posture(raw, a, b, t_rel)
    res["重定向→机器人"] = s2
    issues.sort(key=lambda x: -x["严重度"])
    res["差距清单"] = issues
    res["_series"] = series
    return res


def _iss(stage, item, before, after, diff, sev, t=None):
    return {"环节": stage, "项": item, "前级": before, "后级": after, "差距": diff, "严重度": round(float(sev), 2), "时刻s": t}


def _disp_issues(stage, d0, d1):
    out = []
    for i, ax in enumerate(("前进", "侧移")):
        x0, x1 = d0[i], d1[i]
        diff = round(x1 - x0, 2)
        ratio = x1 / x0 if abs(x0) >= 0.5 else None
        lo, hi = TH["位移比"]
        bad_ratio = ratio is not None and not lo <= ratio <= hi
        if abs(diff) >= TH["位移差m"] or bad_ratio:
            txt = f"差 {diff:+.2f} m" + (f"（{ratio * 100:.0f}%）" if ratio is not None else "")
            out.append(_iss(stage, f"{ax}位移", f"{x0} m", f"{x1} m", txt, abs(diff) / TH["位移差m"]))
    return out


def _yaw_issues(stage, n0, n1, curve, t):
    out = []
    if abs(n1 - n0) >= TH["偏航净差deg"]:
        r = f"（{n1 / n0 * 100:.0f}%）" if abs(n0) >= 90 else ""
        out.append(_iss(stage, "偏航净转角", f"{n0}°", f"{n1}°", f"差 {n1 - n0:+.1f}°{r}", abs(n1 - n0) / TH["偏航净差deg"]))
    if abs(curve) >= TH["偏航曲线差deg"]:
        out.append(_iss(stage, "偏航过程", "", "", f"最大差 {curve:+.1f}°", abs(curve) / TH["偏航曲线差deg"], t))
    return out


def turns(yz) -> dict:
    """先转的方向转了多少（相对起点到最远处），再往回转了多少（最远处到之后的另一个极值），单位 °。"""
    yz = np.asarray(yz, float)
    k = int(np.argmax(np.abs(yz)))
    first = float(yz[k])
    rest = yz[k:]
    back = float(rest.max() - first) if first < 0 else float(rest.min() - first)
    return {"先转": round(first, 1), "回转": round(back, 1)}


def _turn_issues(stage, t0, t1):
    """先转和回转各比一次，前级 ≥ 90° 才算。"""
    out = []
    lo, hi = TH["转动比"]
    for k in ("先转", "回转"):
        a, b = t0[k], t1[k]
        if abs(a) >= 90:
            ratio = b / a
            if not lo <= ratio <= hi:
                name = ("右转" if a < 0 else "左转") + "幅度"
                out.append(_iss(stage, name, f"{abs(a):.0f}°", f"{abs(b):.0f}°", f"{ratio * 100:.0f}%", abs(1 - ratio) / (1 - lo)))
    return out


def public(res: dict) -> dict:
    """去掉画图用的大数组，给前端和 JSON 用。"""
    return {k: v for k, v in res.items() if not k.startswith("_")}
