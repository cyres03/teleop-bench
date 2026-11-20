"""长测看护：持续站立、长时遥操等按时长考核的场景，自动计时并检测异常，算 HPI / HPF。

数据来源：
- 机器人状态：5000 端口的 notify_robot_info（不用登录）。状态变成 Damp* / *Exception 记一条“异常停机”待确认事件。
- 姿态（可选）：SSH 到主控读 /ImuData 和 /joint/state（见 robotstream.py）。
  骨盆 |roll| 或 |pitch| 超过阈值并持续 0.2 s 记一条“疑似摔倒”待确认事件，阈值默认 60°，和主控 FallDetector 一致。
  同时算胸腔摆幅（2 s 峰峰值，目标 ≤4°）和稳定时间 Ts（点“松指令”后腿部关节静止 1 s）。
平地场景有摔倒记 F1，一票否决。
自动检测到的事件先进“待确认”，由人确认算摔倒、算 issue 或忽略，确认后才计数（issue 要和产品对齐）。
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import threading
import time
from dataclasses import dataclass, field

import websockets

from . import db
from .metrics import f1_veto, indicator
from .robotstream import RobotStream, SwayMeter, TsMeter
from .robot import WS_URL, parse_robot_info

EULER_RE = re.compile(r'"euler"\s*:\s*\[\s*([-\d.eE]+)\s*,\s*([-\d.eE]+)\s*,\s*([-\d.eE]+)')
ABNORMAL_STATUS = re.compile(r"damp|exception|fall|error", re.I)


def parse_euler_deg(line: str) -> tuple[float, float, float] | None:
    """mrostopic echo /ImuData 的一行 → (roll, pitch, yaw) 度。"""
    m = EULER_RE.search(line)
    if not m:
        return None
    return tuple(math.degrees(float(x)) for x in m.groups())  # type: ignore[return-value]


@dataclass
class TiltDetector:
    threshold_deg: float = 60.0
    hold_s: float = 0.2
    recover_deg: float = 20.0
    _over_since: float | None = None
    _latched: bool = False

    def feed(self, t: float, roll: float, pitch: float) -> bool:
        """返回 True 表示这一刻判定为一次新的疑似摔倒。恢复到 recover_deg 以内才会再次触发。"""
        tilt = max(abs(roll), abs(pitch))
        if self._latched:
            if tilt < self.recover_deg:
                self._latched = False
                self._over_since = None
            return False
        if tilt >= self.threshold_deg:
            if self._over_since is None:
                self._over_since = t
            if t - self._over_since >= self.hold_s:
                self._latched = True
                return True
        else:
            self._over_since = None
        return False


@dataclass
class WatchEvent:
    id: int
    t: float
    kind: str  # 疑似摔倒 / 异常停机 / 手动
    detail: str
    verdict: str = "待确认"  # 摔倒 / issue / 忽略 / 待确认

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class WatchSession:
    scene: str = ""
    simulated: bool = False
    robot: str = ""
    version: str = ""
    use_imu: bool = False
    threshold_deg: float = 60.0
    state: str = "idle"  # idle / running / paused / finished
    started_at: float = 0
    active_s: float = 0
    _resumed_at: float = 0
    robot_status: str = ""
    last_tilt: tuple[float, float] | None = None
    imu_rate: float = 0
    joint_rate: float = 0
    flat: bool = True
    layer: str = "L3"
    stream_error: str = ""
    sway: SwayMeter = field(default_factory=SwayMeter)
    ts: TsMeter = field(default_factory=TsMeter)
    saved_measure_ids: list[int] = field(default_factory=list)
    events: list[WatchEvent] = field(default_factory=list)
    status_log: list[dict] = field(default_factory=list)
    note: str = ""
    saved_record_ids: list[int] = field(default_factory=list)

    def elapsed_s(self) -> float:
        extra = time.time() - self._resumed_at if self.state == "running" else 0
        return self.active_s + extra

    def counts(self) -> tuple[int, int]:
        falls = sum(1 for e in self.events if e.verdict == "摔倒")
        issues = sum(1 for e in self.events if e.verdict in ("摔倒", "issue"))
        return falls, issues

    def as_dict(self) -> dict:
        hours = self.elapsed_s() / 3600
        falls, issues = self.counts()
        hpf = indicator("HPF", round(hours, 2), falls)
        hpi = indicator("HPI", round(hours, 2), issues)
        return {
            "scene": self.scene, "simulated": self.simulated, "robot": self.robot, "version": self.version,
            "use_imu": self.use_imu, "threshold_deg": self.threshold_deg, "state": self.state,
            "started_at": self.started_at, "elapsed_s": self.elapsed_s(), "robot_status": self.robot_status,
            "last_tilt": self.last_tilt, "imu_rate": self.imu_rate, "joint_rate": self.joint_rate,
            "flat": self.flat, "layer": self.layer, "f1": f1_veto("HPF", falls, self.flat),
            "stream_error": self.stream_error, "sway": self.sway.stats(), "ts": self.ts.stats(),
            "ts_pending": self.ts.pending, "ts_results": self.ts.results[-20:],
            "saved_measure_ids": self.saved_measure_ids,
            "events": [e.as_dict() for e in self.events], "status_log": self.status_log[-50:],
            "falls": falls, "issues": issues,
            "hpf": {"display": hpf.display, "ratio": hpf.ratio}, "hpi": {"display": hpi.display, "ratio": hpi.ratio},
            "pending": sum(1 for e in self.events if e.verdict == "待确认"),
            "saved_record_ids": self.saved_record_ids,
        }


class WatchController:
    def __init__(self, ssh_factory=None):
        self.session = WatchSession()
        self._tasks: list[asyncio.Task] = []
        self._stop = threading.Event()
        self._ssh_factory = ssh_factory  # () -> paramiko.SSHClient，测试时可替换
        self._stream: RobotStream | None = None
        self._last_t: float | None = None

    # ---- 生命周期 ----
    def start(self, *, scene: str, simulated: bool, robot: str, version: str, use_imu: bool,
              threshold_deg: float, flat: bool = True) -> WatchSession:
        if self.session.state in ("running", "paused"):
            raise ValueError("已有看护在进行")
        if not scene.strip():
            raise ValueError("请填写场景名称")
        now = time.time()
        self.session = WatchSession(scene=scene.strip(), simulated=simulated, robot=robot, version=version,
                                    use_imu=use_imu, threshold_deg=threshold_deg, state="running",
                                    started_at=now, _resumed_at=now, flat=flat)
        self._stop.clear()
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._status_loop())]
        self._stream = None
        if use_imu or simulated:
            self._start_stream()
            self._tasks.append(loop.create_task(self._rate_loop()))
        return self.session

    def pause(self) -> None:
        s = self.session
        if s.state == "running":
            s.active_s += time.time() - s._resumed_at
            s.state = "paused"
            s.sway.skip()

    def resume(self) -> None:
        s = self.session
        if s.state == "paused":
            s._resumed_at = time.time()
            s.state = "running"

    def finish(self, note: str = "") -> WatchSession:
        s = self.session
        if s.state not in ("running", "paused"):
            raise ValueError("没有进行中的看护")
        if any(e.verdict == "待确认" for e in s.events):
            raise ValueError("还有待确认的事件，先逐条确认再结束")
        self.pause()
        s.state = "finished"
        s.note = note
        self._stop.set()
        if self._stream:
            self._stream.stop()
        for t in self._tasks:
            t.cancel()
        if not s.simulated:
            hours = round(s.active_s / 3600, 2)
            falls, issues = s.counts()
            for metric, events, issue_type in (("HPF", falls, "摔倒"), ("HPI", issues, "体验问题（含摔倒）")):
                rec = db.add_record(scene=s.scene, issue_type=issue_type, metric=metric, total=hours, events=events,
                                    robot=s.robot, version=s.version, source="长测看护", note=note,
                                    layer=s.layer, flat=s.flat)
                s.saved_record_ids.append(rec["id"])
            for m in self.measure_rows():
                s.saved_measure_ids.append(db.add_measure(**m)["id"])
        return s

    def measure_rows(self) -> list[dict]:
        s = self.session
        rows = []
        sw = s.sway.stats()
        base = dict(scene=s.scene, layer=s.layer, version=s.version, robot=s.robot, source="长测看护")
        if sw["n"]:
            for ax, label in (("roll", "胸腔摆幅 roll"), ("pitch", "胸腔摆幅 pitch")):
                rows.append(dict(base, metric=label, unit="°", value=sw[ax]["median"], max=sw[ax]["max"], n=sw["n"],
                                 target=sw["target"], note=f"2 s 峰峰值中位，{sw['basis']}；超 4° 的窗口 {sw[ax]['over']} 个"))
        ts = s.ts.stats()
        if ts["stable"]:
            rows.append(dict(base, metric="稳定时间 Ts", unit="s", value=ts["median"], max=ts["max"], n=ts["n"],
                             note=f"中位；{ts['timeout']} 次 {s.ts.timeout_s:g} s 内没稳定"))
        return rows

    def mark_release(self) -> None:
        """操作员松指令的时刻，开始等腿部关节静止。"""
        s = self.session
        if s.state != "running":
            raise ValueError("看护没在进行")
        if not self._stream or not (s.joint_rate or s.simulated):
            raise ValueError("没有关节数据：开始时要勾选读取姿态和关节")
        s.ts.mark(self._last_t or time.time())

    # ---- 事件 ----
    def add_event(self, kind: str, detail: str, verdict: str = "待确认") -> WatchEvent:
        ev = WatchEvent(len(self.session.events) + 1, time.time(), kind, detail, verdict)
        self.session.events.append(ev)
        return ev

    def judge(self, event_id: int, verdict: str) -> None:
        if verdict not in ("摔倒", "issue", "忽略"):
            raise ValueError("只能判定为 摔倒 / issue / 忽略")
        for e in self.session.events:
            if e.id == event_id:
                e.verdict = verdict
                return
        raise ValueError("没有这条事件")

    # ---- 数据源 ----
    async def _status_loop(self) -> None:
        s = self.session
        if s.simulated:
            s.robot_status = "Walk"
            while not self._stop.is_set():
                await asyncio.sleep(1)
            return
        while not self._stop.is_set():
            try:
                async with websockets.connect(WS_URL, open_timeout=5) as ws:
                    while not self._stop.is_set():
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                        if msg.get("title") != "notify_robot_info":
                            continue
                        status = parse_robot_info(msg.get("data", {})).get("robot_status", "")
                        if status and status != s.robot_status:
                            s.status_log.append({"t": time.time(), "status": status})
                            if s.robot_status and ABNORMAL_STATUS.search(status) and s.state == "running":
                                self.add_event("异常停机", f"机器人状态 {s.robot_status} → {status}")
                            s.robot_status = status
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                s.robot_status = f"连接中断：{exc.__class__.__name__}"
                await asyncio.sleep(3)

    def _start_stream(self) -> None:
        s = self.session
        det = TiltDetector(threshold_deg=s.threshold_deg)
        self._last_t = None

        def on_tilt(t, cr, cp, pr, pp, moving):
            self._last_t = t
            s.last_tilt = (round(pr, 1), round(pp, 1))
            if s.state != "running":
                return
            if det.feed(t, pr, pp):
                s.sway.skip()
                self.add_event("疑似摔倒", f"roll {pr:.1f}°，pitch {pp:.1f}°，超过 {s.threshold_deg:g}°")
            elif max(abs(pr), abs(pp)) < 20:
                s.sway.feed(t, cr, cp, moving)

        def on_joint(t, vmax):
            self._last_t = t
            res = s.ts.feed(t, vmax)
            if res and not res["ok"]:
                self.add_event("Ts 超时", f"松指令后 {s.ts.timeout_s:g} s 内腿部关节没静止 1 s")

        self._stream = RobotStream(ssh_factory=self._ssh_factory, simulated=s.simulated, on_tilt=on_tilt,
                                   on_joint=on_joint)
        self._stream.start()

    async def _rate_loop(self) -> None:
        s = self.session
        while not self._stop.is_set():
            s.imu_rate, s.joint_rate = self._stream.rates()
            s.stream_error = self._stream.error
            await asyncio.sleep(1)

    def inject_fall(self) -> None:
        """演练：模拟一次倾倒，走和真机一样的检测流程。"""
        if not self.session.simulated:
            raise ValueError("只有演练模式能注入")
        self.add_event("疑似摔倒", "演练注入：roll 72.0°，pitch 48.0°")
        self.session.sway.skip()
