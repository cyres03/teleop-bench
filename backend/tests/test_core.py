import asyncio

import pytest

from bench import db
from bench.latency import parse_output
from bench.metrics import indicator, meets_target
from bench.stress import StressController
from bench.watch import TiltDetector, WatchController, parse_euler_deg


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEOP_BENCH_HOME", str(tmp_path))
    db.init()


# ---------- 指标 ----------
@pytest.mark.parametrize("metric,total,events,display,ratio", [
    ("CPI", 26, 6, "4.3", "(26/6)"),
    ("CPI", 136, 0, "136+", "(136/0)"),
    ("CPI", 136, 136, "1", "(136/136)"),
    ("CPI", 100, 42, "2.4", "(100/42)"),
    ("CPI", 100, 3, "33.3", "(100/3)"),
    ("HPF", 24, 0, "24+", "(24/0)"),
    ("HPI", 1.5, 2, "0.8", "(1.5/2)"),
])
def test_indicator_format(metric, total, events, display, ratio):
    ind = indicator(metric, total, events)
    assert (ind.display, ind.ratio) == (display, ratio)


def test_indicator_rules():
    assert indicator("CPI", 3, 1).small_sample
    assert not indicator("HPF", 2, 0).small_sample
    with pytest.raises(ValueError):
        indicator("CPI", 2.5, 1)
    with pytest.raises(ValueError):
        indicator("XYZ", 1, 1)
    assert meets_target(indicator("CPI", 120, 0), 100) is True
    assert meets_target(indicator("CPI", 30, 0), 100) is False
    assert meets_target(indicator("CPI", 100, 42), 100) is False
    assert meets_target(indicator("CPI", 10, 1), None) is None


def test_ledger_roundtrip_and_markdown():
    db.add_record(scene="Move 稳定性", issue_type="摔倒", metric="CPI-F", total=136, events=0)
    db.add_record(scene="Mode 切换至 Stand", issue_type="切换失败", metric="CPI", total=3, events=1)
    recs = db.list_records()
    assert [r["display"] for r in recs] == ["3", "136+"]
    md = db.ledger_markdown(recs)
    assert "Move 稳定性（摔倒），CPI-F = 136+（136/0）" in md
    assert "单点参考" in md
    with pytest.raises(ValueError):
        db.add_record(scene="", issue_type="x", metric="CPI", total=1, events=0)


# ---------- 模式切换压测 ----------
class ScriptedRobot:
    simulated = False
    accid = "HU_D04_01_TEST"

    def __init__(self, fail_on: set[int]):
        self.calls = []
        self.fail_on = fail_on

    async def request(self, title, data=None, timeout=30):
        self.calls.append(title)
        await asyncio.sleep(0)
        return {"result": "fail" if len(self.calls) in self.fail_on else "success"}


def _run(ctrl, robot, **kw):
    async def go():
        ctrl.start(robot, **kw)
        while ctrl.run.running:
            await asyncio.sleep(0.01)
    asyncio.run(go())


def test_stress_counts_and_saves():
    robot = ScriptedRobot(fail_on={4})  # 每个循环 3 个调用，第 4 个是第二次进动作库
    ctrl = StressController()
    _run(ctrl, robot, sequence="engine", cycles=3, settle_s=0, hanging=True, site_confirmed=True,
         continue_on_fail=True)
    summary = {s["step"]: s for s in ctrl.run.summary()}
    assert summary["engine_on"]["total"] == 3 and summary["engine_on"]["fails"] == 1
    assert summary["engine_on"]["outcomes"] == "101"
    assert summary["walk"]["display"] == "3+"
    recs = db.list_records()
    assert len(recs) == 3  # 两步各一条 CPI，整个序列一条 CPI-F
    assert {r["metric"] for r in recs} == {"CPI", "CPI-F"} and all(r["layer"] == "L4" for r in recs)


def test_stress_stops_on_failure_by_default():
    robot = ScriptedRobot(fail_on={1})
    ctrl = StressController()
    _run(ctrl, robot, sequence="engine", cycles=5, settle_s=0, hanging=True, site_confirmed=True,
         continue_on_fail=False)
    assert robot.calls == ["request_set_motion_engine"]
    assert ctrl.run.end_reason.endswith("失败后停止")


def test_stress_safety_gates():
    ctrl = StressController()
    robot = ScriptedRobot(set())

    async def go(**kw):
        ctrl.start(robot, **kw)
    with pytest.raises(ValueError, match="吊装"):
        asyncio.run(go(sequence="sit", cycles=1, settle_s=0, hanging=True, site_confirmed=True, continue_on_fail=False))
    with pytest.raises(ValueError, match="现场安全"):
        asyncio.run(go(sequence="engine", cycles=1, settle_s=0, hanging=True, site_confirmed=False,
                       continue_on_fail=False))


# ---------- 长测看护 ----------
def test_euler_parse_and_tilt_detector():
    line = '[2026-11-06 16:23:36.014] /ImuData (x)[13]: {"euler":[1.0472, 0.1, 0.0],"quat":[1,0,0,0]}'
    roll, pitch, _ = parse_euler_deg(line)
    assert round(roll) == 60 and round(pitch) == 6
    det = TiltDetector(threshold_deg=60, hold_s=0.2)
    assert not det.feed(0.0, 65, 0)
    assert det.feed(0.25, 65, 0)
    assert not det.feed(0.5, 70, 0)  # 已触发，未恢复前不重复
    assert not det.feed(0.6, 5, 0)
    assert not det.feed(1.0, 61, 0)
    assert det.feed(1.3, 61, 0)


def test_watch_requires_judging_before_finish():
    async def go():
        ctrl = WatchController()
        ctrl.start(scene="持续站立稳定性", simulated=False, robot="HU_D04_01_TEST", version="v", use_imu=False,
                   threshold_deg=60)
        ev = ctrl.add_event("疑似摔倒", "roll 70°")
        with pytest.raises(ValueError, match="待确认"):
            ctrl.finish()
        ctrl.judge(ev.id, "摔倒")
        ctrl.add_event("手动", "抖动", verdict="issue")
        s = ctrl.finish("测试")
        return s
    s = asyncio.run(go())
    assert s.counts() == (1, 2)
    recs = {r["metric"]: r for r in db.list_records()}
    assert recs["HPF"]["events"] == 1 and recs["HPI"]["events"] == 2


# ---------- 视频延迟 ----------
SAMPLE = """A：902 帧 30.1 fps，闪烁块 (29, 0, 91, 90, '反相', 2365)，翻转 76 次
B：902 帧 30.1 fps，闪烁块 (0, 0, 160, 90)，翻转 77 次
互相关粗估 B−A：-2 ms；配上 76 次翻转
延迟 ms：均值 -2.1（±2.8，95% 置信），中位数 -1.6，标准差 12.3，P5 -20.9，P95 17.7，最小 -26.3，最大 21.4
单次读数受两路帧间隔量化，误差最大 ±33 ms；翻转时刻随机，量化误差在均值里抵消，延迟以均值为准
"""


def test_parse_latency_output():
    r = parse_output(SAMPLE)
    assert r["ok"] and r["mean"] == -2.1 and r["p95"] == 17.7 and r["pairs"] == 76
    assert r["A"]["flips"] == 76 and r["B"]["fps"] == 30.1
    assert "warning" not in r
    bad = parse_output("A：894 帧 29.8 fps，闪烁块 (0,0,160,90)，翻转 80 次\nB：538 帧 17.9 fps，闪烁块 (16,0,80,57)，翻转 12 次\n"
                       "互相关粗估 B−A：1368 ms；配上 5 次翻转\n延迟 ms：均值 1379.7（±34.4，95% 置信），中位数 1368.1，标准差 38.5，"
                       "P5 1338.7，P95 1431.0，最小 1337.2，最大 1435.5")
    assert bad["ok"] and "warning" in bad
    assert parse_output("报错了")["ok"] is False
