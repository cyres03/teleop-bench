#!/usr/bin/env python3
"""腿部运动学里程计：用 /joint/state 正运动学 + /ImuData 姿态，假设较低的那只脚（支撑脚）不动，估骨盆水平位移。
统计每次松杆后机器人还走了多远、多久。
用法：python3 leg_odom.py <bag> [--json out.json]
几何取自 HU_D04_01.urdf（24.7.4 包）；脚底到踝关节的偏置不计，只影响支撑脚判定，不影响位移。
"""
import argparse
import bisect
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rosbag_lite as r  # noqa: E402

# (原点 xyz, 轴)，左腿；右腿 y 取反、绕 x/z 的轴取反
CHAIN_L = [((0.017007, 0.1283, 0.0081917), (0, 0.90631, -0.42262)),
           ((-0.01, 0.0016596, -0.0920417), (1, 0, 0)),
           ((0, 0, -0.1774), (0, 0, 1)),
           ((0, 0, -0.1976), (0, 1, 0)),
           ((0, 0, -0.385), (0, 1, 0)),
           ((0, 0, 0), (1, 0, 0))]
LEGS = {'L': (['left_hip_pitch_joint', 'left_hip_roll_joint', 'left_hip_yaw_joint', 'left_knee_joint',
               'left_ankle_pitch_joint', 'left_ankle_roll_joint'], 1),
        'R': (['right_hip_pitch_joint', 'right_hip_roll_joint', 'right_hip_yaw_joint', 'right_knee_joint',
               'right_ankle_pitch_joint', 'right_ankle_roll_joint'], -1)}
MOVE_V = 0.05     # 骨盆水平速度低于此值算停下，m/s
HOLD = 0.5        # 连续低于多久算停下，s
POST = 8.0        # 松杆后最多看多久，s


def rot(axis, a):
    x, y, z = axis
    n = math.sqrt(x * x + y * y + z * z)
    x, y, z = x / n, y / n, z / n
    c, s, C = math.cos(a), math.sin(a), 1 - math.cos(a)
    return [[c + x * x * C, x * y * C - z * s, x * z * C + y * s],
            [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
            [z * x * C - y * s, z * y * C + x * s, c + z * z * C]]


def mm(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def mv(a, v):
    return [sum(a[i][k] * v[k] for k in range(3)) for i in range(3)]


def chain(side):
    m = LEGS[side][1]
    out = []
    for (x, y, z), (ax, ay, az) in CHAIN_L:
        if m < 0:
            y, ax, az = -y, -ax, -az
        out.append(((x, y, z), (ax, ay, az)))
    return out


CH = {'L': chain('L'), 'R': chain('R')}


def foot(side, q):
    R = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    p = [0.0, 0.0, 0.0]
    for (o, ax), a in zip(CH[side], q):
        d = mv(R, o)
        p = [p[i] + d[i] for i in range(3)]
        R = mm(R, rot(ax, a))
    return p


def quat_mat(w, x, y, z):
    return [[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('bag')
    ap.add_argument('--json')
    ap.add_argument('--step', type=int, default=4, help='关节帧抽样间隔')
    a = ap.parse_args()

    imu, js, cmd, idx = [], [], [], None
    for tp, t, m in r.Bag(a.bag).read(['/ImuData', '/joint/state', '/cmd_vel']):
        if tp == '/ImuData':
            imu.append((m['header']['stamp'], t, m['quat']))
        elif tp == '/joint/state':
            if idx is None:
                idx = {s: [m['names'].index(n) for n in LEGS[s][0]] for s in 'LR'}
            js.append((m['header']['stamp'], t, {s: [m['q'][i] for i in idx[s]] for s in 'LR'}))
        else:
            cmd.append((t, m['linear']['x'], m['linear']['y'], m['angular']['z']))
    oi = min(x[1] - x[0] for x in imu)
    oj = min(x[1] - x[0] for x in js)
    imu = sorted((x[0] + oi, x[2]) for x in imu)
    it = [x[0] for x in imu]
    js = sorted(((x[0] + oj, x[2]) for x in js), key=lambda x: x[0])[::a.step]
    js = [x for x in js if it[0] <= x[0] <= it[-1]]

    # 骨盆水平位置（世界系），按支撑脚不动积分
    pos, px, py, stance, prev = [], 0.0, 0.0, None, None
    for t, q in js:
        Rw = quat_mat(*imu[min(bisect.bisect_left(it, t), len(imu) - 1)][1])
        fw = {s: mv(Rw, foot(s, q[s])) for s in 'LR'}
        low = 'L' if fw['L'][2] < fw['R'][2] else 'R'
        if stance is None or (low != stance and abs(fw['L'][2] - fw['R'][2]) > 0.01):
            stance = low
            prev = fw[stance]
        else:
            f = fw[stance]
            px -= f[0] - prev[0]
            py -= f[1] - prev[1]
            prev = f
        pos.append((t, px, py, stance))
    pt = [p[0] for p in pos]

    def at(t):
        i = min(max(bisect.bisect_left(pt, t), 0), len(pos) - 1)
        return pos[i]

    def yaw(t):
        w, x, y, z = imu[min(bisect.bisect_left(it, t), len(imu) - 1)][1]
        return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))

    # 按 /cmd_vel 方向切段（任意时长），统计推杆中和松杆后的位移
    segs, cur = [], None
    for t, vx, vy, wz in cmd:
        k = (1 if vx > 0.05 else -1 if vx < -0.05 else 0, 1 if vy > 0.05 else -1 if vy < -0.05 else 0,
             1 if wz > 0.05 else -1 if wz < -0.05 else 0)
        if k != (0, 0, 0) and cur and cur['k'] == k and t - cur['t1'] < 0.3:
            cur['t1'] = t
        elif k != (0, 0, 0):
            cur = {'k': k, 't0': t, 't1': t}
            segs.append(cur)
    out = []
    for n, g in enumerate(segs):
        if g['k'][2] != 0:   # 转身段不看位移
            continue
        tb = g['t1']
        tn = segs[n + 1]['t0'] if n + 1 < len(segs) else math.inf
        te = min(tb + POST, tn, pt[-1])
        h = yaw(g['t0'])
        ch, sh = math.cos(h), math.sin(h)

        def fwd(p0, p1):
            dx, dy = p1[1] - p0[1], p1[2] - p0[2]
            return dx * ch + dy * sh, -dx * sh + dy * ch

        a0, a1 = at(g['t0']), at(tb)
        walk = fwd(a0, a1)
        # 松杆后：直到骨盆水平速度连续 HOLD 秒 < MOVE_V
        stop, quiet, last = None, None, at(tb)
        steps = 0
        st = last[3]
        for p in pos[bisect.bisect_left(pt, tb):bisect.bisect_right(pt, te)]:
            if p[3] != st:
                steps += 1
                st = p[3]
            dt = p[0] - last[0]
            if dt >= 0.1:
                v = math.hypot(p[1] - last[1], p[2] - last[2]) / dt
                last = p
                if v < MOVE_V:
                    quiet = p[0] if quiet is None else quiet
                    if p[0] - quiet >= HOLD:
                        stop = quiet
                        break
                else:
                    quiet = None
        tend = stop if stop is not None else te
        post = fwd(a1, at(tend))
        out.append({'seg': n, 't0': round(g['t0'] - pt[0], 2), 'dur': round(tb - g['t0'], 2), 'dir': g['k'][:2],
                    'walk_fwd': round(walk[0], 2), 'walk_lat': round(walk[1], 2),
                    'post_s': None if stop is None else round(stop - tb, 2), 'post_window': round(te - tb, 2),
                    'post_fwd': round(post[0], 2), 'post_lat': round(post[1], 2), 'post_steps': steps})
    name = {(1, 0): '前进', (-1, 0): '后退', (0, 1): '左移', (0, -1): '右移'}
    print(os.path.basename(a.bag))
    for s in out:
        print(f"  {name.get(tuple(s['dir']), str(s['dir']))} t={s['t0']:7.2f}s 推杆 {s['dur']:5.2f}s 位移 前{s['walk_fwd']:+.2f} 侧{s['walk_lat']:+.2f} m | "
              f"松杆后 {'未停' if s['post_s'] is None else str(s['post_s']) + 's 停'}（看了 {s['post_window']}s）"
              f" 又走 前{s['post_fwd']:+.2f} 侧{s['post_lat']:+.2f} m，换脚 {s['post_steps']} 次")
    if a.json:
        json.dump(out, open(a.json, 'w'), ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
