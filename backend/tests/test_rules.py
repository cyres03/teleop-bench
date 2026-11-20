"""《Oli 遥操跟随性能与稳定性测试方案》规则：F1 一票否决、基线与回退、执行节奏、摆幅、Ts、压测分段、L4 边界。"""
import asyncio
import gzip
import math
from pathlib import Path

import pytest

from bench import db
from bench.boundary import BoundaryController, summarize
from bench.robotstream import RobotStream, SwayMeter, TsMeter
from bench.stress import StressController

FIX = Path(__file__).parent / "fixtures" / "659_fwd1_31-41s.txt.gz"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEOP_BENCH_HOME", str(tmp_path))
    db.init()


def test_flat_fall_is_f1_veto_even_with_target_met():
    db.add_record(scene="平地行走", issue_type="摔倒", metric="CPI-F", total=100, events=1, target=50, version="v2",
                  layer="L2")
    db.add_record(scene="斜坡", issue_type="摔倒", metric="CPI-F", total=100, events=1, target=50, version="v2",
                  layer="L4", flat=False)
    by = {r["scene"]: r for r in db.list_records()}
    assert by["平地行走"]["status"] == "f1"
    assert by["斜坡"]["status"] == "pass"


def test_first_version_is_baseline_and_regression_flagged():
    db.add_record(scene="遥操", issue_type="体验", metric="CPI", total=30, events=2, version="v1", created_at=1)
    db.add_record(scene="遥操", issue_type="体验", metric="CPI", total=30, events=5, version="v2", created_at=2)
    db.add_record(scene="站立", issue_type="体验", metric="CPI", total=30, events=2, version="v1", created_at=1)
    db.add_record(scene="站立", issue_type="体验", metric="CPI", total=40, events=0, version="v2", created_at=2)
    rows = {(r["scene"], r["version"]): r for r in db.list_records()}
    assert rows[("遥操", "v1")]["status"] == "baseline"
    assert rows[("遥操", "v2")]["status"] == "regress" and rows[("遥操", "v2")]["prev_display"] == "15"
    assert rows[("站立", "v2")]["status"] == "none"  # 0 问题不算回退
    assert "比上一版回退" in db.ledger_markdown(db.list_records())


def test_measures_lower_is_better_with_10pct_tolerance():
    db.add_measure(scene="行走", metric="胸腔摆幅 pitch", unit="°", value=1.00, version="v1", created_at=1)
    db.add_measure(scene="行走", metric="胸腔摆幅 pitch", unit="°", value=1.09, version="v2", created_at=2)
    db.add_measure(scene="行走", metric="胸腔摆幅 pitch", unit="°", value=1.30, version="v3", created_at=3)
    st = {r["version"]: r["status"] for r in db.list_measures()}
    assert st == {"v1": "baseline", "v2": "none", "v3": "regress"}


def test_cadence_per_version_and_weekly():
    db.add_record(scene="a", issue_type="x", metric="CPI", total=5, events=0, version="v2", layer="L2")
    db.add_record(scene="b", issue_type="x", metric="HPF", total=5, events=0, version="v2", layer="L3",
                  created_at=1.0)
    c = {x["layer"]: x for x in db.cadence("v2")}
    assert not c["L2"]["due"] and c["L4"]["due"] and c["L5"]["due"]
    assert c["L3"]["due"]  # 上次很久以前
    assert not c["L1"]["due"]


def _replay_fixture(on_tilt, on_joint):
    s = RobotStream(ssh_factory=None, simulated=False, on_tilt=on_tilt, on_joint=on_joint)
    with gzip.open(FIX, "rt") as f:
        for line in f:
            kind, rest = line[0], line[2:]
            (s._imu_line if kind == "I" else s._joint_line)(rest)


def test_sway_and_ts_on_real_659_walk():
    """659 24.7.4 直行第 1 趟 31–41 s（37.4 s 停下），按实时流同样的降采样回放。"""
    sway = SwayMeter()
    joints = []
    _replay_fixture(lambda t, cr, cp, pr, pp, m: sway.feed(t, cr, cp, m), lambda t, v: joints.append((t, v)))
    sway._close()
    st = sway.stats()
    assert st["basis"] == "运动中窗口" and st["n"] >= 3
    assert 1.0 < st["pitch"]["median"] < 3.0 and st["roll"]["median"] < 1.5  # 和 analyze_imu_sway.py 同量级
    assert st["pass"] is True
    last_move = max(t for t, v in joints if v >= 0.3)
    ts = TsMeter()
    ts.mark(last_move - 0.8)
    res = [r for t, v in joints if (r := ts.feed(t, v))]
    assert res and res[0]["ok"] and 0.6 < res[0]["ts"] < 1.1


def test_ts_timeout_and_sway_target():
    ts = TsMeter(timeout_s=2)
    ts.mark(0)
    out = [r for i in range(200) if (r := ts.feed(i * 0.02, 1.0))]
    assert out and out[0]["ok"] is False
    sw = SwayMeter()
    for i in range(400):
        t = i * 0.02
        sw.feed(t, 0.2 * math.sin(6 * t), 3.0 * math.sin(6 * t), True)  # pitch 峰峰值 6°
    sw._close()
    st = sw.stats()
    assert st["pitch"]["median"] > 4 and st["pass"] is False and st["pitch"]["over"] == st["n"]


class _Robot:
    simulated = False
    accid = "HU_TEST"

    def __init__(self):
        self.calls = 0

    async def request(self, title, data):
        self.calls += 1
        await asyncio.sleep(0)
        return {"result": "success"}


def test_stress_blocks_of_20_and_fall_is_f1():
    ctrl = StressController()
    robot = _Robot()

    async def go():
        ctrl.start(robot, sequence="engine", cycles=45, settle_s=0, hanging=True, site_confirmed=True,
                   continue_on_fail=False, version="v2")
        while ctrl.run.running:
            await asyncio.sleep(0)
            if ctrl.run.cycle == 30 and not ctrl.run.falls:
                ctrl.record_fall("侧摔")
    asyncio.run(go())
    r = ctrl.run.as_dict()
    assert r["end_reason"] == "摔倒，F1 一票否决"
    assert [b["range"] for b in r["blocks"]] == ["1–20", "21–30"]
    assert r["blocks"][0]["ok"] and r["blocks"][1]["falls"] == 1
    recs = [x for x in db.list_records() if x["metric"] == "CPI-F"]
    assert len(recs) == 1 and recs[0]["status"] == "f1" and recs[0]["layer"] == "L4" and recs[0]["total"] == 30


def test_stand_lie_move_sequence_exists_and_needs_no_hanging():
    from bench.stress import SEQUENCES
    seq = SEQUENCES["slm"]
    assert [s.label for s in seq["steps"]] == ["躺下", "起身到 Stand", "进 Move"] and not seq["hanging_ok"]


def test_boundary_rules():
    b = BoundaryController()
    b.bind(robot="HU_TEST", version="v2", simulated=False)
    with pytest.raises(ValueError, match="饱和要写清边界"):
        b.result(action="高抬腿", result="饱和")
    b.result(action="高抬腿", result="完成")
    b.result(action="高抬腿", result="饱和", boundary="抬膝到胸前一半")
    b.set_controller("LBMotion")
    b.result(action="高抬腿", result="摔倒")
    rows = {r["controller"]: r for r in summarize(db.list_trials(b.key))}
    assert rows["WBT"]["completion"] == 100 and not rows["WBT"]["enough"] and not rows["WBT"]["f1"]
    assert rows["LBMotion"]["f1"] and rows["LBMotion"]["cpif"] == "1"
    b.save_to_ledger()
    b.save_to_ledger()  # 再点一次是更新
    recs = db.list_records()
    assert len(recs) == 4
    lb = [r for r in recs if "LBMotion" in r["scene"] and r["metric"] == "CPI-F"][0]
    assert lb["status"] == "f1" and lb["layer"] == "L4"
    wbt = [r for r in recs if "WBT" in r["scene"] and r["metric"] == "CPI-F"][0]
    assert "抬膝到胸前一半" in wbt["note"]


class _FakeSSH:
    def __init__(self, lines):
        self.lines = lines

    def exec_command(self, cmd, get_pty=False):
        import io
        return None, io.StringIO("".join(self.lines)), None

    def close(self):
        pass


def test_stream_reports_missing_publisher_and_no_data(monkeypatch):
    import time as _t
    s = RobotStream(ssh_factory=lambda: _FakeSSH(["Wait a moment...\n", "ERROR: No found topic[/ImuData], please try again.\n"]),
                    simulated=False, joints=False)
    s.start()
    _t.sleep(0.3)
    assert "No found topic" in s.error
    s2 = RobotStream(ssh_factory=lambda: _FakeSSH([]), simulated=False, joints=False)
    s2.start()
    s2.started_at -= 20
    s2.rates()
    assert "没收到 /ImuData" in s2.error


def test_latency_report_compares_previous_version():
    from bench import report
    ok = lambda m: {"ok": True, "mean": m, "p5": m - 10, "p95": m + 10, "median": m, "pairs": 80}
    db.add_latency_run("measure", "a", ok(80.0), link="近场 H264", version="v1")
    db.add_latency_run("measure", "b", ok(95.0), link="近场 H264", version="v2")
    db.add_latency_run("measure", "c", ok(70.0), link="近场 JPEG", version="v2")
    pv = report.prev_version("近场 H264", "v2")
    assert pv == ("v1", 80.0) and report.regressed(95.0, pv)
    assert report.prev_version("近场 JPEG", "v2") is None and not report.regressed(70.0, None)
    assert report.prev_version("近场 H264", "v1") is None


def test_lint_blocks_subjective_wording(tmp_path):
    """报告里不能有主观判断，评价词必须带数据（11-09 Cyres / Billow）。"""
    import subprocess, sys
    tool = Path(__file__).parents[1] / "tools" / "lint_report_style.py"
    bad = tmp_path / "bad.txt"
    bad.write_text("# 对比三组配置并结合操作员的口头反馈，综合表现最好的是第二组\n第二组更跟手。\n", encoding="utf-8")
    good = tmp_path / "good.txt"
    good.write_text("第二组任务成功率 10/10，每次耗时中位 12.4 s，表现最好。\n本体感觉回传延迟 120 ms。\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(tool), "--file", str(bad)], capture_output=True, text=True)
    assert r.returncode == 1 and "主观判断" in r.stdout and "评价没有数据" in r.stdout
    r = subprocess.run([sys.executable, str(tool), "--file", str(good)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
