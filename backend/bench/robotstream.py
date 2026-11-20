"""机器人姿态和关节数据流，以及按《Oli 遥操跟随性能与稳定性测试方案》口径算的两个指标。

数据：SSH 到主控 mrostopic echo，机器人上先降采样再传，避免把热点堵满。
- /ImuData（500 Hz）每 10 帧取 1 帧，约 50 Hz；IMU 装在骨盆。
- /joint/state（约 950 Hz）每 20 帧取 1 帧，约 48 Hz；字段 names、q、v。
两个话题的 header.stamp 不是同一时钟（659 上差约 1.9 s），所以统一用 echo 行首的接收时间（主控时钟）对齐。

指标：
- 摆幅：胸腔 roll / pitch 每 2 s 不重叠窗口的峰峰值，取中位和最大，目标 ≤4°。
  胸腔姿态 = 骨盆 IMU · Rz(waist_yaw) · Rx(waist_roll) · Ry(waist_pitch)（HU_D04_01.urdf 腰部串联链）。
  没有关节数据时退回骨盆姿态。窗口内一半以上时刻有腿部关节角速度 ≥0.3 rad/s 记为“运动中”，判定只看运动中的窗口；
  一个运动窗口都没有时用全部窗口。
- 稳定时间 Ts：松指令后 12 个腿部关节角速度都 <0.3 rad/s 并连续 1 s；Ts = 松指令到这段静止开始的时间。
"""
from __future__ import annotations

import datetime
import json
import math
import random
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

MROS = "source /opt/limx/install/setup.bash >/dev/null 2>&1; export MROS_IP_LIST=10.192.1.x; "
IMU_EVERY, JOINT_EVERY = 10, 20
WAIST_JOINTS = ("waist_yaw_joint", "waist_roll_joint", "waist_pitch_joint")
LEG_KEYS = ("hip", "knee", "ankle")
SWAY_WINDOW_S = 2.0
SWAY_TARGET_DEG = 4.0
TS_VEL = 0.3
TS_HOLD_S = 1.0
TS_TIMEOUT_S = 10.0


def echo_cmd(topic: str, every: int) -> str:
    """ERROR 行（例如话题没有发布者）原样透传，方便界面提示。"""
    return (f"{MROS}mrostopic echo {topic} 2>&1"
            f" | awk 'index($0,\"ERROR\"){{print; fflush()}} index($0,\"{{\"){{n++; if(n%{every}==0){{print; fflush()}}}}'")


# ---------- 旋转（同 scripts/analyze_imu_sway.py） ----------
def quat_to_matrix(w, x, y, z):
    n = math.sqrt(w * w + x * x + y * y + z * z) or 1.0
    w, x, y, z = w / n, x / n, y / n, z / n
    return ((1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)),
            (2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)),
            (2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)))


def _rx(a):
    c, s = math.cos(a), math.sin(a)
    return ((1, 0, 0), (0, c, -s), (0, s, c))


def _ry(a):
    c, s = math.cos(a), math.sin(a)
    return ((c, 0, s), (0, 1, 0), (-s, 0, c))


def _rz(a):
    c, s = math.cos(a), math.sin(a)
    return ((c, -s, 0), (s, c, 0), (0, 0, 1))


def _mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def rpy_to_matrix(roll, pitch, yaw):
    return _mul(_mul(_rz(yaw), _ry(pitch)), _rx(roll))


def roll_pitch_deg(m) -> tuple[float, float]:
    return math.degrees(math.atan2(m[2][1], m[2][2])), math.degrees(math.asin(max(-1.0, min(1.0, -m[2][0]))))


def chest_roll_pitch(pelvis, waist: tuple[float, float, float] | None) -> tuple[float, float]:
    if waist is None:
        return roll_pitch_deg(pelvis)
    y, r, p = waist
    return roll_pitch_deg(_mul(_mul(_mul(pelvis, _rz(y)), _rx(r)), _ry(p)))


# ---------- 解析 echo 行 ----------
def receipt_time(line: str) -> float | None:
    """行首 [YYYY-mm-dd HH:MM:SS.fff] → 秒。"""
    if not line.startswith("[") or len(line) < 25:
        return None
    try:
        return datetime.datetime.strptime(line[1:24], "%Y-%m-%d %H:%M:%S.%f").timestamp()
    except ValueError:
        return None


def _json(line: str) -> dict | None:
    i = line.find("{")
    if i < 0:
        return None
    try:
        return json.loads(line[i:])
    except json.JSONDecodeError:
        return None


def parse_imu(line: str):
    """→ (t, 骨盆旋转矩阵)。"""
    msg = _json(line)
    if not msg:
        return None
    q = msg.get("quat")
    if q and len(q) == 4 and any(q):
        m = quat_to_matrix(*map(float, q))
    elif msg.get("euler"):
        e = msg["euler"]
        m = rpy_to_matrix(float(e[0]), float(e[1]), float(e[2]) if len(e) > 2 else 0.0)
    else:
        return None
    return receipt_time(line), m


def parse_joint(line: str):
    """→ (t, 腰关节 (yaw, roll, pitch) 弧度或 None, 腿部关节最大 |角速度| rad/s 或 None)。"""
    msg = _json(line)
    if not msg:
        return None
    names = msg.get("names") or msg.get("name") or []
    q = msg.get("q") or msg.get("position") or []
    v = msg.get("v") or msg.get("velocity") or []
    try:
        waist = tuple(float(q[names.index(n)]) for n in WAIST_JOINTS)
    except (ValueError, IndexError):
        waist = None
    legs = [abs(float(v[i])) for i, n in enumerate(names) if i < len(v) and any(k in n for k in LEG_KEYS)]
    return receipt_time(line), waist, (max(legs) if legs else None)


# ---------- 指标 ----------
@dataclass
class SwayMeter:
    window_s: float = SWAY_WINDOW_S
    target_deg: float = SWAY_TARGET_DEG
    windows: list[dict] = field(default_factory=list)
    _buf: list[tuple[float, float, float, bool | None]] = field(default_factory=list)

    def feed(self, t: float, roll: float, pitch: float, moving: bool | None) -> None:
        if self._buf and t - self._buf[0][0] >= self.window_s:
            self._close()
        self._buf.append((t, roll, pitch, moving))

    def skip(self) -> None:
        """丢掉当前未满的窗口（暂停、疑似摔倒时用）。"""
        self._buf = []

    def _close(self) -> None:
        b = self._buf
        self._buf = []
        if len(b) < 5 or b[-1][0] - b[0][0] < self.window_s / 2:
            return
        flags = [m for *_, m in b if m is not None]
        self.windows.append({
            "t": b[0][0],
            "roll": max(x[1] for x in b) - min(x[1] for x in b),
            "pitch": max(x[2] for x in b) - min(x[2] for x in b),
            "moving": (sum(flags) >= len(flags) / 2) if flags else None,
        })

    def stats(self) -> dict:
        moving = [w for w in self.windows if w["moving"]]
        use = moving or self.windows
        if not use:
            return {"n": 0, "n_total": len(self.windows), "basis": "", "pass": None}
        out: dict = {"n": len(use), "n_total": len(self.windows), "basis": "运动中窗口" if moving else "全部窗口",
                     "target": self.target_deg}
        for ax in ("roll", "pitch"):
            vals = [w[ax] for w in use]
            out[ax] = {"median": round(statistics.median(vals), 2), "max": round(max(vals), 2),
                       "over": sum(v > self.target_deg for v in vals),
                       "max_all": round(max(w[ax] for w in self.windows), 2)}
        out["pass"] = max(out["roll"]["median"], out["pitch"]["median"]) <= self.target_deg
        return out


@dataclass
class TsMeter:
    vel: float = TS_VEL
    hold_s: float = TS_HOLD_S
    timeout_s: float = TS_TIMEOUT_S
    results: list[dict] = field(default_factory=list)
    _mark: float | None = None
    _still: float | None = None

    @property
    def pending(self) -> bool:
        return self._mark is not None

    def mark(self, t: float) -> None:
        self._mark, self._still = t, None

    def feed(self, t: float, max_leg_v: float) -> dict | None:
        if self._mark is None or t < self._mark:
            return None
        if max_leg_v < self.vel:
            if self._still is None:
                self._still = t
            if t - self._still >= self.hold_s:
                res = {"t": self._mark, "ts": round(self._still - self._mark, 2), "ok": True}
                self.results.append(res)
                self._mark = self._still = None
                return res
        else:
            self._still = None
        if t - self._mark > self.timeout_s + self.hold_s:
            res = {"t": self._mark, "ts": None, "ok": False}
            self.results.append(res)
            self._mark = self._still = None
            return res
        return None

    def stats(self) -> dict:
        ok = [r["ts"] for r in self.results if r["ok"]]
        return {"n": len(self.results), "stable": len(ok), "timeout": len(self.results) - len(ok),
                "median": round(statistics.median(ok), 2) if ok else None, "max": round(max(ok), 2) if ok else None}


# ---------- 数据流 ----------
class RobotStream:
    """在后台线程读 IMU 和关节，回调 on_tilt(t, 胸腔 roll, 胸腔 pitch, 骨盆 roll, 骨盆 pitch, moving) 和 on_joint(t, max_leg_v)。

    t 用主控接收时间；拿不到时用电脑时间。演练模式生成走路样的合成数据。
    """

    def __init__(self, *, ssh_factory: Callable | None, simulated: bool, joints: bool = True,
                 on_tilt: Callable | None = None, on_joint: Callable | None = None):
        self.ssh_factory, self.simulated, self.joints = ssh_factory, simulated, joints
        self.on_tilt, self.on_joint = on_tilt, on_joint
        self.stop_evt = threading.Event()
        self.waist: tuple[float, float, float] | None = None
        self.moving: bool | None = None
        self.imu_times: list[float] = []
        self.joint_times: list[float] = []
        self.error = ""
        self._client = None
        self.started_at = 0.0
        self.got_imu = self.got_joint = False

    NO_DATA_S = 15

    def rates(self) -> tuple[float, float]:
        now = time.time()
        if not self.error and not self.simulated and self.started_at and now - self.started_at > self.NO_DATA_S:
            if not self.got_imu:
                self.error = f"{self.NO_DATA_S} s 没收到 /ImuData：确认机器人运控已启动（主控上 mrostopic list 能看到不代表有发布者）"
            elif self.joints and not self.got_joint:
                self.error = f"{self.NO_DATA_S} s 没收到 /joint/state：摆幅只能按骨盆算，Ts 测不了"
        self.imu_times = [x for x in self.imu_times if now - x < 5]
        self.joint_times = [x for x in self.joint_times if now - x < 5]
        return len(self.imu_times) / 5, len(self.joint_times) / 5

    def start(self) -> None:
        self.started_at = time.time()
        if self.simulated:
            threading.Thread(target=self._sim, daemon=True).start()
            return
        self._client = self.ssh_factory()
        threading.Thread(target=self._read, args=("/ImuData", IMU_EVERY, self._imu_line), daemon=True).start()
        if self.joints:
            threading.Thread(target=self._read, args=("/joint/state", JOINT_EVERY, self._joint_line), daemon=True).start()

    def stop(self) -> None:
        self.stop_evt.set()
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass

    def _read(self, topic: str, every: int, handle: Callable[[str], None]) -> None:
        try:
            _, stdout, _ = self._client.exec_command(echo_cmd(topic, every), get_pty=True)
            for line in iter(stdout.readline, ""):
                if self.stop_evt.is_set():
                    break
                if "ERROR" in line and "{" not in line:
                    self.error = f"{topic}：{line.strip()[:160]}"
                    continue
                handle(line)
        except Exception as exc:
            if not self.stop_evt.is_set():
                self.error = f"{topic} 读取中断：{exc.__class__.__name__}"

    def _imu_line(self, line: str) -> None:
        r = parse_imu(line)
        if not r:
            return
        t, pelvis = r
        t = t or time.time()
        self.got_imu = True
        self.imu_times.append(time.time())
        pr, pp = roll_pitch_deg(pelvis)
        cr, cp = chest_roll_pitch(pelvis, self.waist)
        if self.on_tilt:
            self.on_tilt(t, cr, cp, pr, pp, self.moving)

    def _joint_line(self, line: str) -> None:
        r = parse_joint(line)
        if not r:
            return
        t, waist, vmax = r
        t = t or time.time()
        self.got_joint = True
        self.joint_times.append(time.time())
        if waist is not None:
            self.waist = waist
        if vmax is not None:
            self.moving = vmax >= TS_VEL
            if self.on_joint:
                self.on_joint(t, vmax)

    def _sim(self) -> None:
        """演练：走 6 s、停 4 s 循环；走时胸腔 pitch 约 ±1.5°、roll ±1°，停下 0.4 s 后腿部静止。"""
        rng = random.Random()
        t0 = time.time()
        while not self.stop_evt.is_set():
            t = time.time()
            ph = (t - t0) % 10
            walking = ph < 6
            settling = 6 <= ph < 6.4
            amp = 1.0 if walking else 0.05
            cr = amp * math.sin(2 * math.pi * 0.9 * t) + rng.gauss(0, 0.05)
            cp = 1.5 * amp * math.sin(2 * math.pi * 1.8 * t) + rng.gauss(0, 0.05)
            vmax = (1.2 + rng.random()) if walking else (0.5 if settling else 0.05)
            self.imu_times.append(t)
            self.moving = vmax >= TS_VEL
            self.joint_times.append(t)
            if self.on_joint:
                self.on_joint(t, vmax)
            if self.on_tilt:
                self.on_tilt(t, cr, cp, cr * 0.8, cp * 0.8, self.moving)
            time.sleep(0.02)
