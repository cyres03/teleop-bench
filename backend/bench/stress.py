"""模式切换压测：按序列循环下发 SDK 切换指令，每一步等 response 成功再走下一步，统计 CPI 和耗时。

安全规则（同 Oli Robot Manager 姿态循环）：
- 吊装保护打开时禁止坐下、躺下类序列；
- 任一步失败或超时默认立即停止，不再下发后续动作；
- “停止”只停后续调度，不打断机器人正在执行的动作。

《Oli 遥操跟随性能与稳定性测试方案》L4 通用边界：模式切换每 20 次统计一段，判据“平滑落地、无摔倒失控”。
现场看到摔倒点“记一次摔倒”：立即停止，按平地摔倒记 F1（CPI-F，一票否决）。
不用 IMU 自动判摔：躺下时骨盆本来就会倾斜到 90° 左右，会误报。
"""
from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field

from . import db
from .metrics import f1_veto, indicator

BLOCK = 20  # 每 20 次循环统计一段


@dataclass(frozen=True)
class Step:
    key: str
    label: str
    scene: str
    calls: tuple[tuple[str, dict], ...]


SEQUENCES: dict[str, dict] = {
    "sit": {
        "label": "坐下 ↔ 起身",
        "hanging_ok": False,
        "settle_s": 5,
        "steps": (
            Step("sit", "坐下", "模式切换至 Sit", (("request_from_stand_to_sit", {}),)),
            Step("stand_from_sit", "起身", "模式切换至 Stand（坐姿起身）",
                 (("request_standup", {"mode": "sitting"}), ("request_set_walk_mode", {}))),
        ),
    },
    "lie": {
        "label": "躺下 ↔ 起身",
        "hanging_ok": False,
        "settle_s": 8,
        "steps": (
            Step("lie", "躺下", "模式切换至 Lie", (("request_lie_down", {}),)),
            Step("stand_from_lie", "起身", "模式切换至 Stand（躺姿起身）",
                 (("request_standup", {"mode": "lying"}), ("request_set_walk_mode", {}))),
        ),
    },
    "engine": {
        "label": "Walk ↔ 动作库",
        "hanging_ok": True,
        "settle_s": 2,
        "steps": (
            Step("engine_on", "进动作库", "模式切换至动作库", (("request_set_motion_engine", {"mode": 1}),)),
            Step("walk", "回 Walk", "模式切换至 Walk",
                 (("request_set_motion_engine", {"mode": 0}), ("request_set_walk_mode", {}))),
        ),
    },
    "slm": {
        "label": "Stand ↔ Lie ↔ Move",
        "hanging_ok": False,
        "settle_s": 8,
        "steps": (
            Step("lie_from_move", "躺下", "模式切换至 Lie（从 Move）", (("request_lie_down", {}),)),
            Step("stand_only", "起身到 Stand", "模式切换至 Stand（躺姿起身）",
                 (("request_standup", {"mode": "lying"}),)),
            Step("move", "进 Move", "模式切换至 Move（从 Stand）", (("request_set_walk_mode", {}),)),
        ),
    },
}


def sequences_public() -> list[dict]:
    return [{"key": k, "label": v["label"], "hanging_ok": v["hanging_ok"], "settle_s": v["settle_s"],
             "steps": [{"key": s.key, "label": s.label, "scene": s.scene} for s in v["steps"]]}
            for k, v in SEQUENCES.items()]


@dataclass
class Attempt:
    cycle: int
    step: str
    ok: bool
    ms: float
    detail: str


@dataclass
class StressRun:
    sequence: str = ""
    cycles: int = 0
    settle_s: float = 0
    simulated: bool = False
    robot: str = ""
    version: str = ""
    running: bool = False
    stop_requested: bool = False
    continue_on_fail: bool = False
    started_at: float = 0
    ended_at: float = 0
    current: str = ""
    end_reason: str = ""
    attempts: list[Attempt] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)
    saved_record_ids: list[int] = field(default_factory=list)
    falls: list[dict] = field(default_factory=list)
    cycle: int = 0
    fall_record_id: int | None = None

    def say(self, text: str, level: str = "info") -> None:
        self.log.append({"t": time.time(), "text": text, "level": level})

    def summary(self) -> list[dict]:
        out = []
        for step in SEQUENCES.get(self.sequence, {}).get("steps", ()):
            mine = [a for a in self.attempts if a.step == step.key]
            if not mine:
                continue
            ms = sorted(a.ms for a in mine if a.ok)
            fails = sum(1 for a in mine if not a.ok)
            ind = indicator("CPI", len(mine), fails)
            out.append({
                "step": step.key, "label": step.label, "scene": step.scene,
                "total": len(mine), "fails": fails, "display": ind.display, "ratio": ind.ratio,
                "outcomes": "".join("1" if a.ok else "0" for a in mine),
                "median_ms": round(statistics.median(ms)) if ms else None,
                "max_ms": round(ms[-1]) if ms else None,
            })
        return out

    def blocks(self) -> list[dict]:
        """每 20 个循环一段：失败、摔倒、耗时中位，看是否随次数劣化。"""
        if not self.attempts:
            return []
        out = []
        last = max(a.cycle for a in self.attempts)
        for start in range(1, last + 1, BLOCK):
            end = start + BLOCK - 1
            mine = [a for a in self.attempts if start <= a.cycle <= end]
            ms = [a.ms for a in mine if a.ok]
            falls = sum(1 for f in self.falls if start <= f["cycle"] <= end)
            cycles = len({a.cycle for a in mine})
            out.append({"range": f"{start}–{min(end, last)}", "cycles": cycles, "steps": len(mine),
                        "fails": sum(1 for a in mine if not a.ok), "falls": falls,
                        "median_ms": round(statistics.median(ms)) if ms else None,
                        "max_ms": round(max(ms)) if ms else None,
                        "ok": falls == 0 and all(a.ok for a in mine)})
        return out

    def cycles_done(self) -> int:
        return len({a.cycle for a in self.attempts})

    def as_dict(self) -> dict:
        return {
            "sequence": self.sequence, "cycles": self.cycles, "settle_s": self.settle_s,
            "simulated": self.simulated, "robot": self.robot, "version": self.version,
            "running": self.running, "stop_requested": self.stop_requested,
            "continue_on_fail": self.continue_on_fail,
            "started_at": self.started_at, "ended_at": self.ended_at, "current": self.current,
            "end_reason": self.end_reason, "done_steps": len(self.attempts),
            "total_steps": self.cycles * len(SEQUENCES.get(self.sequence, {}).get("steps", ())),
            "summary": self.summary(), "log": self.log[-200:], "saved_record_ids": self.saved_record_ids,
            "blocks": self.blocks(), "block_size": BLOCK, "falls": self.falls, "cycles_done": self.cycles_done(),
            "f1": f1_veto("CPI-F", len(self.falls), True),
        }


class StressController:
    def __init__(self):
        self.run = StressRun()
        self._task: asyncio.Task | None = None

    def start(self, robot, *, sequence: str, cycles: int, settle_s: float | None, hanging: bool,
              site_confirmed: bool, continue_on_fail: bool, version: str = "") -> StressRun:
        if self.run.running:
            raise ValueError("已有压测在运行")
        if sequence not in SEQUENCES:
            raise ValueError("未知序列")
        seq = SEQUENCES[sequence]
        if not robot.simulated and not site_confirmed:
            raise ValueError("请先确认现场安全条件")
        if hanging and not seq["hanging_ok"]:
            raise ValueError(f"吊装保护打开时不能跑“{seq['label']}”")
        if not 1 <= cycles <= 500:
            raise ValueError("循环次数要在 1–500 之间")
        self.run = StressRun(sequence=sequence, cycles=cycles,
                             settle_s=seq["settle_s"] if settle_s is None else max(0.0, float(settle_s)),
                             simulated=robot.simulated, robot=robot.accid, version=version,
                             continue_on_fail=continue_on_fail, running=True, started_at=time.time())
        self.run.say(f"开始：{seq['label']}，{cycles} 个循环，每步后等 {self.run.settle_s:g} s"
                     + ("（演练）" if robot.simulated else ""))
        self._task = asyncio.get_running_loop().create_task(self._loop(robot))
        return self.run

    def stop(self) -> None:
        if self.run.running:
            self.run.stop_requested = True
            self.run.say("已请求停止：当前一步完成后不再下发", "warn")

    def record_fall(self, detail: str = "") -> None:
        """现场看到摔倒：记下所在循环和步骤，立即停止后续调度。"""
        run = self.run
        if not run.running and not run.attempts:
            raise ValueError("没有进行中的压测")
        cycle = run.cycle or 1
        run.falls.append({"t": time.time(), "cycle": cycle, "at": run.current or "结束后", "detail": detail})
        run.say(f"记一次摔倒（第 {cycle} 次循环）：平地摔倒记 F1，停止压测", "error")
        if run.running:
            run.stop_requested = True
            run.end_reason = "摔倒，F1 一票否决"
        elif not run.simulated:
            self._save_falls(run)

    async def _loop(self, robot) -> None:
        run = self.run
        steps = SEQUENCES[run.sequence]["steps"]
        settle = run.settle_s * (0.2 if robot.simulated else 1.0)
        try:
            for cycle in range(1, run.cycles + 1):
                for step in steps:
                    if run.stop_requested:
                        run.end_reason = run.end_reason or "手动停止"
                        return
                    run.cycle = cycle
                    run.current = f"第 {cycle}/{run.cycles} 次 · {step.label}"
                    ok, ms, detail = await self._do_step(robot, step)
                    run.attempts.append(Attempt(cycle, step.key, ok, ms, detail))
                    if ok:
                        run.say(f"{run.current}：成功，{ms / 1000:.1f} s")
                    else:
                        run.say(f"{run.current}：失败，{detail}", "error")
                        if not run.continue_on_fail:
                            run.end_reason = f"{step.label}失败后停止"
                            return
                    await asyncio.sleep(settle)
            run.end_reason = "全部完成" if not run.falls else run.end_reason
        except Exception as exc:  # 保底：任何异常都停下，不再发指令
            run.end_reason = f"异常停止：{exc}"
            run.say(run.end_reason, "error")
        finally:
            run.running = False
            run.current = ""
            run.ended_at = time.time()
            run.say(f"结束：{run.end_reason}")
            self._save(run)

    @staticmethod
    async def _do_step(robot, step: Step) -> tuple[bool, float, str]:
        t0 = time.monotonic()
        for title, data in step.calls:
            try:
                result = await robot.request(title, data)
            except Exception as exc:
                return False, (time.monotonic() - t0) * 1000, str(exc)
            if result.get("result") != "success":
                return False, (time.monotonic() - t0) * 1000, f"{title} 返回 {result.get('result', result)}"
        return True, (time.monotonic() - t0) * 1000, ""

    @staticmethod
    def _save(run: StressRun) -> None:
        if run.simulated:
            run.say("演练结果不写入台账")
            return
        for item in run.summary():
            rec = db.add_record(
                scene=item["scene"], issue_type="切换失败", metric="CPI", total=item["total"],
                events=item["fails"], robot=run.robot, version=run.version, source="模式切换压测",
                outcomes=item["outcomes"], layer="L4",
                note=f"耗时中位 {item['median_ms']} ms，最长 {item['max_ms']} ms；{run.end_reason}",
            )
            run.saved_record_ids.append(rec["id"])
        StressController._save_falls(run)
        if run.saved_record_ids:
            run.say(f"已写入台账 {len(run.saved_record_ids)} 条")

    @staticmethod
    def _save_falls(run: StressRun) -> None:
        """整个序列一条 CPI-F：总数 = 完成的循环数，摔倒次数；平地，有摔倒即 F1。"""
        if run.simulated or not run.cycles_done():
            return
        if run.fall_record_id:
            db.delete_record(run.fall_record_id)
            run.saved_record_ids = [i for i in run.saved_record_ids if i != run.fall_record_id]
        seq = SEQUENCES[run.sequence]["label"]
        rec = db.add_record(scene=f"模式切换压测：{seq}", issue_type="摔倒", metric="CPI-F",
                            total=run.cycles_done(), events=len(run.falls), robot=run.robot, version=run.version,
                            source="模式切换压测", layer="L4", flat=1,
                            note="；".join(f"第 {f['cycle']} 次 {f['at']} {f['detail']}".strip() for f in run.falls))
        run.fall_record_id = rec["id"]
        if rec["id"] not in run.saved_record_ids:
            run.saved_record_ids.append(rec["id"])
