"""全身遥操回放：分析算法、回放包制作和改时间戳、演练全流程出报告。"""
import time

import pytest

from bench.lbm import rosbag_lite as rl
from bench.wbt import analyze as an
from bench.wbt import pack
from bench.wbt.runner import DEMO_BAG, WbtController, pack_path


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEOP_BENCH_HOME", str(tmp_path))


def test_analyze_sample_bag_splits_loss_by_stage():
    """660 右行样本：重定向几乎复现人，机器人横移只做到约三成。"""
    r = an.analyze_bag(str(DEMO_BAG))
    assert r["follow_s"] == pytest.approx(11.4, abs=0.2)
    assert r["控制器"] == "AvatarQ"
    d = r["人→重定向"]["位移"]
    assert abs(d["人"]["侧移"] - d["重定向"]["侧移"]) < 0.05
    rob = r["重定向→机器人"]["位移"]["机器人"]["侧移"]
    assert 0.2 < rob / d["重定向"]["侧移"] < 0.5
    top = [i for i in r["差距清单"] if i["环节"] == "重定向→机器人"][0]
    assert top["项"] == "侧移位移"
    assert r["差距清单"] == sorted(r["差距清单"], key=lambda i: -i["严重度"])
    assert len(r["重定向→机器人"]["关节"]) == 31


def test_pack_has_entry_exit_buttons_and_restamps_to_now(tmp_path):
    rows = [(t, m) for _, t, m in rl.Bag(str(pack_path("左行"))).read(["/vr_cmd"])]
    t0 = rows[0][0]

    def pressed(lo, hi):
        return {k for t, m in rows if lo <= t - t0 < hi for k in ("LG", "RG", "A", "B", "X", "Y") if m[k]}

    assert pressed(0, 0.55) == set()
    assert pressed(0.65, 0.95) == {"RG", "A"}
    assert "A" not in pressed(1.0, rows[-1][0] - t0)          # 动作里的 A/B/X/Y 已清掉，只有开头一次 RG+A
    assert pressed(rows[-1][0] - t0 - 0.9, rows[-1][0] - t0 - 0.75) == {"LG", "RG", "B"}
    assert pressed(rows[-1][0] - t0 - 0.5, 99) == set()
    seqs = [m["header"]["seq"] for _, m in rows[:200]]
    assert all(b - a == 1 for a, b in zip(seqs, seqs[1:150]))  # 补段 seq 连续

    now = int(time.time() * 1e9)
    out = tmp_path / "now.bag"
    pack.restamp(str(pack_path("左行")), str(out), now)
    n = 0
    for tp, t, m in rl.Bag(str(out)).read(["/vr_cmd", "/tf"]):
        stamps = [m["header"]["stamp"]] if tp == "/vr_cmd" else [x["header"]["stamp"] for x in m["transforms"]]
        assert all(abs(s - t) < 1e-6 for s in stamps)
        n += 1
    assert n > 3000
    assert next(rl.Bag(str(out)).read(["/vr_cmd"]))[1] == pytest.approx(now / 1e9, abs=1e-6)


def test_simulated_run_produces_report(tmp_path):
    w = WbtController()
    w.bind(robot="SIM", version="0", simulated=True)
    for _ in range(50):
        if w.prep.get("ok"):
            break
        time.sleep(0.1)
    w.begin("左行")
    for _ in range(300):
        if not w.run["active"]:
            break
        time.sleep(0.1)
    assert not w.error
    runs = w.runs()
    assert len(runs) == 1 and not runs[0]["aborted"]
    assert w.image(runs[0]["id"]) is not None
    path, lint = w.write_report()
    assert lint["errors"] == 0
    text = open(path, encoding="utf-8").read()
    assert "## 差距清单：重定向→机器人" in text and "侧移位移" in text


def test_history_keeps_only_baseline_replays_latest_per_version(tmp_path):
    """版本对比只收播当前基线包的真机结果；同一版本同一机器人取最新一次；演练和离线分析不算。"""
    import json
    from bench.wbt.runner import pack_info
    res = an.public(an.analyze_bag(str(DEMO_BAG)))
    md5 = pack_info("左行")["md5"]

    def run(t, ver, pmd5, aborted=False):
        return {"id": 1, "item": "左行", "name": t, "time": t, "pack_md5": pmd5, "aborted": aborted,
                "version": ver, "robot": "660", "result": res}

    for d, rows in {"2026-11-20_660": [run("10:00:00", "A", md5), run("11:00:00", "A", md5), run("12:00:00", "A", md5, True)],
                    "2026-11-21_660": [run("09:00:00", "B", md5), run("10:00:00", "B", "")],
                    "演练_2026-11-21_SIM": [run("09:00:00", "C", md5)]}.items():
        (tmp_path / "wbt" / d).mkdir(parents=True)
        (tmp_path / "wbt" / d / "runs.json").write_text(json.dumps(rows), encoding="utf-8")
    h = WbtController().history()
    assert [(x["version"], x["time"]) for x in h] == [("A", "11:00:00"), ("B", "09:00:00")]
    assert h[0]["主方向"] == "侧移" and 0.2 < h[0]["完成率"] < 0.5


def test_turn_pack_is_only_the_spins():
    """转身包只截原地转圈：先右转约两圈再左转回来，骨盆几乎不平移。"""
    import numpy as np
    y, p = [], []
    for _, _, m in rl.Bag(str(pack_path("转身"))).read(["/tf"]):
        for x in m["transforms"]:
            if x["child_frame_id"] == "/vr_body/Pelvis":
                y.append(an.yaw_of(an._q(x["transform"]["rotation"])))
                p.append((x["transform"]["translation"]["x"], x["transform"]["translation"]["y"]))
    t = an.turns(np.degrees(np.unwrap(y)) - np.degrees(y[0]))
    assert t["先转"] < -700 and t["回转"] > 700
    assert np.hypot(*(np.array(p[-1]) - p[0])) < 0.5


def test_turn_issues_flag_under_rotation():
    assert an.turns([0, -300, -720, -400, 10, 5]) == {"先转": -720.0, "回转": 730.0}
    iss = an._turn_issues("重定向→机器人", {"先转": -714.0, "回转": 735.0}, {"先转": -601.0, "回转": 700.0})
    assert [i["项"] for i in iss] == ["右转幅度"]


def test_aborted_run_dropped_from_report_but_record_kept(tmp_path):
    import json
    res = an.public(an.analyze_bag(str(DEMO_BAG)))
    w = WbtController()
    w.bind(robot="SIM", version="0", simulated=True)
    rows = [{"id": 1, "item": "左行", "name": "1120_100000_左行", "time": "10:01:00", "bag": "a.bag", "md5": "", "aborted": False, "result": res},
            {"id": 2, "item": "右行", "name": "1120_110000_右行", "time": "11:01:00", "bag": "b.bag", "md5": "", "aborted": True, "ask": True, "result": res}]
    (w._dir() / "runs.json").write_text(json.dumps(rows), encoding="utf-8")
    path, _ = w.write_report()
    text = open(path, encoding="utf-8").read()
    assert "10:00:00 左行" in text and "中途停止，待确认是否计入" in text and (w._dir() / "img" / "关节.png").exists()
    w.keep(2, False)
    text = open(path, encoding="utf-8").read()
    assert "11:00:00 右行" not in text
    assert len(w.runs()) == 2 and w.runs()[1]["excluded"]
