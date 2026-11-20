"""L4 功能边界探测（《Oli 遥操跟随性能与稳定性测试方案》）。

主从前提三条：
1. 操作员做任何动作，控制器都要能跟随；
2. 跟随到一定边界后不再跟随（饱和 / 安全拒绝）可接受，但边界必须可枚举——选“饱和”必须写清边界；
3. 平地无障碍时机器人绝不能因不稳定摔倒——一票否决，摔倒记 F1 入 CPI-F。

动作集取自《Oli遥操-脚本v4》，VR 全身动捕模式下逐项探边界，每动作 ≥3 次，天车保护。
一次 = 按“边界执行”做完一组（例如高抬手 ×10）。每次记结果：完成 / 饱和 / 未跟随 / 摔倒。
可选读姿态：每次从“开始这次”到记结果之间，算胸腔 2 s 峰峰值摆幅的最大值。
同一天同一台机器人上切换控制器（WBT / LBMotion）记录，自动出双控制器对比。
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from . import db
from .metrics import MIN_TRIALS, f1_veto, indicator
from .robotstream import RobotStream, SwayMeter

ACTIONS = [
    {"key": "高抬手", "how": "双臂从体侧快速高抬，最大幅度 ×10", "watch": "手臂关节 RMSE、跟随完成度、躯干摆幅",
     "floor": "不摔；记录饱和边界"},
    {"key": "翻转双手", "how": "掌心上下连续翻转 ×20", "watch": "末端翻转跟随延迟、完成度", "floor": "不摔；记录高频小动作边界"},
    {"key": "下压腿", "how": "单腿前下压踢出，左右各 ×10", "watch": "腿 / 踝关节 RMSE、单腿支撑摆幅", "floor": "不摔；记录单腿支撑边界"},
    {"key": "深蹲", "how": "蹲至最低点后站起，连续 ×10", "watch": "下蹲深度、最低点摆幅、起身 Ts", "floor": "不摔"},
    {"key": "高抬腿", "how": "左右交替抬膝至胸前，各 ×10", "watch": "单腿支撑稳定性、摆幅峰值", "floor": "不摔；记录抬腿高度 / 频率边界"},
    {"key": "转体", "how": "左右连续大幅旋转 ≥720°", "watch": "终点朝向偏差、摆幅峰值", "floor": "不摔"},
    {"key": "弯腰触地", "how": "弯腰伸手触地 ×5", "watch": "躯干前俯极限、触地达成率", "floor": "不摔"},
    {"key": "摘帽前抛", "how": "抬手摘帽并向前抛出（上肢组合）", "watch": "组合跟随完成度、松手时机偏差", "floor": "不摔；边界单独记录"},
    {"key": "控制权交接", "how": "VR 断线重登、双操作员轮换接管", "watch": "残留动作、接管延迟（备注里写秒数）",
     "floor": "无残留动作"},
    {"key": "启停边界", "how": "低电量启动、急停触发与恢复", "watch": "状态机行为", "floor": "状态机行为一致"},
]
ACTION_KEYS = {a["key"] for a in ACTIONS}
RESULTS = ("完成", "饱和", "未跟随", "摔倒")
CONTROLLERS = ("WBT", "LBMotion")


@dataclass
class BoundaryState:
    controller: str = "WBT"
    simulated: bool = False
    robot: str = ""
    version: str = ""
    active: str = ""  # 正在做的动作
    active_since: float = 0
    stream_on: bool = False
    stream_error: str = ""
    sway: SwayMeter = field(default_factory=SwayMeter)
    tilt_peak: float = 0
    saved_record_ids: list[int] = field(default_factory=list)


def session_key(robot: str, simulated: bool) -> str:
    return ("演练 " if simulated else "") + time.strftime("%Y-%m-%d") + " " + (robot or "未识别")


def summarize(trials: list[dict]) -> list[dict]:
    """按（控制器，动作）汇总：次数、完成度、饱和边界、未跟随、摔倒、CPI-F、摆幅峰值。"""
    out = []
    for ctrl in CONTROLLERS:
        for a in ACTIONS:
            mine = [t for t in trials if t["controller"] == ctrl and t["action"] == a["key"]]
            if not mine:
                continue
            n = len(mine)
            c = {r: sum(1 for t in mine if t["result"] == r) for r in RESULTS}
            cpif = indicator("CPI-F", n, c["摔倒"])
            sway = [float(t["sway"]) for t in mine if t["sway"]]
            out.append({
                "controller": ctrl, "action": a["key"], "n": n, **{f"n_{r}": v for r, v in c.items()},
                "completion": round((c["完成"] + c["饱和"]) / n * 100),
                "boundaries": [t["boundary"] for t in mine if t["boundary"]],
                "cpif": cpif.display, "cpif_ratio": cpif.ratio,
                "f1": f1_veto("CPI-F", c["摔倒"], True),
                "enough": n >= MIN_TRIALS,
                "sway_max": max(sway) if sway else None,
                "sway_median": round(statistics.median(sway), 2) if sway else None,
            })
    return out


class BoundaryController:
    def __init__(self, ssh_factory=None):
        self.s = BoundaryState()
        self._ssh_factory = ssh_factory
        self._stream: RobotStream | None = None

    def bind(self, *, robot: str, version: str, simulated: bool) -> None:
        if self.s.robot != robot or self.s.simulated != simulated:
            self.stream(False)
            self.s = BoundaryState(controller=self.s.controller, robot=robot, version=version, simulated=simulated)
        self.s.version = version

    @property
    def key(self) -> str:
        return session_key(self.s.robot, self.s.simulated)

    def state(self) -> dict:
        trials = db.list_trials(self.key)
        s = self.s
        live = s.sway.stats() if s.active else None
        return {
            "session": self.key, "controller": s.controller, "controllers": CONTROLLERS, "actions": ACTIONS,
            "results": RESULTS, "simulated": s.simulated, "robot": s.robot, "version": s.version,
            "active": s.active, "active_since": s.active_since, "stream_on": s.stream_on,
            "stream_error": s.stream_error, "live_sway": live, "tilt_peak": round(s.tilt_peak, 1),
            "rates": self._stream.rates() if self._stream else (0, 0),
            "trials": trials, "summary": summarize(trials), "min_trials": MIN_TRIALS,
            "saved_record_ids": s.saved_record_ids,
        }

    def set_controller(self, controller: str) -> None:
        if controller not in CONTROLLERS:
            raise ValueError(f"控制器只能是 {' / '.join(CONTROLLERS)}")
        if self.s.active:
            raise ValueError("这次还没记结果，记完再换控制器")
        self.s.controller = controller

    def stream(self, on: bool) -> None:
        s = self.s
        if not on:
            if self._stream:
                self._stream.stop()
            self._stream = None
            s.stream_on = False
            return
        if self._stream:
            return

        def on_tilt(t, cr, cp, pr, pp, moving):
            if s.active:
                s.sway.feed(t, cr, cp, None)
                s.tilt_peak = max(s.tilt_peak, abs(pr), abs(pp))

        self._stream = RobotStream(ssh_factory=self._ssh_factory, simulated=s.simulated, joints=True, on_tilt=on_tilt)
        self._stream.start()
        s.stream_on = True

    def begin(self, action: str) -> None:
        if action not in ACTION_KEYS:
            raise ValueError("没有这个动作")
        s = self.s
        s.active, s.active_since, s.tilt_peak = action, time.time(), 0
        s.sway = SwayMeter()

    def result(self, *, action: str, result: str, boundary: str = "", note: str = "") -> dict:
        s = self.s
        if result not in RESULTS:
            raise ValueError(f"结果只能是 {' / '.join(RESULTS)}")
        if action not in ACTION_KEYS:
            raise ValueError("没有这个动作")
        if result == "饱和" and not boundary.strip():
            raise ValueError("饱和要写清边界（例如抬腿高度到胸前一半、翻转频率 >2 Hz 不跟）")
        sway = ""
        if s.active == action and s.stream_on:
            s.sway._close()
            if s.sway.windows:
                sway = f"{max(max(w['roll'], w['pitch']) for w in s.sway.windows):.2f}"
            if s.tilt_peak >= 60 and result != "摔倒":
                note = (note + "；" if note else "") + f"骨盆倾角峰值 {s.tilt_peak:.0f}°，请复核是否摔倒"
        rec = db.add_trial(session=self.key, action=action, result=result, boundary=boundary.strip(),
                           note=note.strip(), sway=sway, controller=s.controller, robot=s.robot, version=s.version)
        s.active = ""
        return rec

    def save_to_ledger(self) -> list[int]:
        s = self.s
        if s.simulated:
            raise ValueError("演练数据不写台账")
        for rid in s.saved_record_ids:  # 再点一次是更新，不重复写
            db.delete_record(rid)
        ids = []
        for row in summarize(db.list_trials(self.key)):
            scene = f"L4 边界：{row['action']}（{row['controller']}）"
            bnd = "；".join(row["boundaries"])
            note = f"完成度 {row['completion']}%，饱和 {row['n_饱和']} 次" + (f"，边界：{bnd}" if bnd else "")
            if row["sway_max"] is not None:
                note += f"；胸腔摆幅峰值 {row['sway_max']}°"
            common = dict(scene=scene, total=row["n"], robot=s.robot, version=s.version, source="边界探测",
                          layer="L4", flat=1, note=note)
            ids.append(db.add_record(issue_type="摔倒", metric="CPI-F", events=row["n_摔倒"], **common)["id"])
            ids.append(db.add_record(issue_type="未跟随（含摔倒）", metric="CPI",
                                     events=row["n_未跟随"] + row["n_摔倒"], **common)["id"])
        if not ids:
            raise ValueError("今天还没有记录")
        s.saved_record_ids = ids
        return ids
