"""LBMotion 推杆测试：分析算法、主控命令、演练全流程、报告规范。"""
import math
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bench.lbm import analyze as an
from bench import wifi
from bench.lbm import runner
from bench.lbm.runner import ITEMS, LbmController, PushCounter, _pick, local_tool_md5, record_cmd, simulated_series, stop_cmd

TOOLS = Path(__file__).resolve().parents[1] / "tools"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEOP_BENCH_HOME", str(tmp_path))


def _straight(vx_cmd=1.0, v_act=0.6, lat=0.05, push=(3.0, 8.0), end=14.0, coast=0.3):
    """匀速直线：推杆期间以 v_act 前进、以 lat 比例向左偏；松杆后线性减速 coast 米。"""
    s = an.Series()
    dt, x, y = 0.02, 0.0, 0.0
    for k in range(int(end / dt)):
        t = k * dt
        on = push[0] <= t < push[1]
        s.cmd.append((t, vx_cmd if on else 0.0, 0.0, 0.0))
        s.lbm.append((t, 0.5 if on else 0.0, 0.0))
        if on:
            v = v_act
        elif push[1] <= t < push[1] + 2 * coast / v_act:
            v = v_act * (1 - (t - push[1]) * v_act / (2 * coast))
        else:
            v = 0.0
        x += v * dt
        y += v * lat * dt
        s.pos.append((t, x, y))
        s.yaw.append((t, 0.0))
        s.knee.append((t, 0.6, 0.6))
    return s


def test_straight_chain_ratios_and_drift():
    seg = an.analyze(_straight())
    assert len(seg) == 1
    m = seg[0]
    assert m["kind"] == "前进" and m["released"] and not m["cut_start"]
    assert m["cmd"] == pytest.approx(1.0)
    assert m["lbm"] == pytest.approx(0.5)
    assert m["actual"] == pytest.approx(0.6, abs=0.01)
    assert m["actual_over_cmd"] == pytest.approx(0.6, abs=0.01)
    assert m["actual_over_lbm"] == pytest.approx(1.2, abs=0.02)
    assert m["travel_m"] == pytest.approx(3.0, abs=0.03)
    assert m["side_rate"] == pytest.approx(0.05, abs=0.002)
    assert m["stop_s"] == pytest.approx(1.0, abs=0.15)
    assert m["coast_main"] == pytest.approx(0.3, abs=0.03)


def test_turn_uses_yaw_and_internal_degrees():
    s = an.Series()
    dt, yaw = 0.02, 0.0
    wz = math.radians(45.8)
    for k in range(int(12 / dt)):
        t = k * dt
        on = 2 <= t < 9
        s.cmd.append((t, 0.0, 0.0, wz if on else 0.0))
        s.lbm.append((t, 0.0, math.radians(34.4) if on else 0.0))
        yaw += (math.radians(29.3) if on else 0.0) * dt
        s.yaw.append((t, yaw))
        s.pos.append((t, 0.0, 0.0))
        s.knee.append((t, 0.6, 0.6))
    m = an.analyze(s)[0]
    assert m["kind"] == "左转" and m["unit"] == "°/s"
    assert m["cmd"] == pytest.approx(45.8, abs=0.05)
    assert m["lbm"] == pytest.approx(34.4, abs=0.05)
    assert m["actual"] == pytest.approx(29.3, abs=0.2)
    assert m["actual_over_cmd"] == pytest.approx(29.3 / 45.8, abs=0.01)
    assert m["stop_s"] is not None and m["stop_s"] < 0.6


def test_lateral_has_no_internal_field():
    s = simulated_series("左移", [(2, 7)], 12)
    m = _pick(an.analyze(s), "左移")
    assert m["lbm"] is None and m["actual_over_lbm"] is None
    assert m["actual"] > 0


def test_push_already_running_at_bag_start_is_flagged_and_skipped():
    s = _straight(push=(0.0, 6.0))
    seg = an.analyze(s)
    assert seg[0]["cut_start"]
    assert _pick(seg, "前进") is None


def test_next_push_before_stop_marks_interrupted():
    s = _straight(end=14.0)
    s.cmd = [c if not (8.2 <= c[0] < 9.0) else (c[0], 0.0, 0.0, 0.5) for c in s.cmd]
    seg = [x for x in an.analyze(s) if x["kind"] == "前进"][0]
    assert seg["stop_s"] is None and seg["interrupted"]


def test_record_command_only_records_needed_topics_and_refuses_busy():
    cmd = record_cmd("1110_前进_1")
    assert "mrosrecord -q -O '1110_前进_1' -a -x" in cmd
    assert "lbmotion_states" in cmd and "joint/state" in cmd
    assert "setsid nohup" in cmd and "BUSY" in cmd
    stop = stop_cmd("1110_前进_1", 123)
    assert "kill -INT -- -123" in stop and "md5sum" in stop and ".bag.active" in stop


def test_bad_name_is_quoted():
    assert "'a;rm -rf ~'" in record_cmd("a;rm -rf ~")


@pytest.mark.parametrize("item", [i["key"] for i in ITEMS])
def test_simulated_series_round_trip(item):
    segs = an.analyze(simulated_series(item, [(2, 7), (12, 17)], 22))
    mine = [x for x in segs if x["kind"] == item]
    assert len(mine) == 2 and all(m["steady"] and m["actual_over_cmd"] > 0.3 for m in mine)


def test_push_counter_counts_only_full_pushes_in_right_direction():
    c = PushCounter("前进")
    assert c.status(0)["phase"] == "准备"
    t = 0.0

    def run(v, dur):
        nonlocal t
        end = t + dur
        while t < end:
            c.feed(t, v)
            t += 0.03

    run((0, 0, 0), 1)
    assert c.status(t)["phase"] == "可以推杆"
    run((1.0, 0, 0), 5.2)
    assert c.status(t)["phase"] == "推杆"
    run((0, 0, 0), 0.5)
    assert c.pushes == [5.2] and c.status(t)["phase"] == "等停"
    run((0, 0, 0), 3)
    run((1.0, 0, 0), 0.3)   # 太短不算
    run((0, 0, 0), 1)
    run((-0.5, 0, 0), 1)    # 反方向
    run((0, 0, 0), 1)
    assert len(c.pushes) == 1 and c.wrong == 1
    assert c.status(t + 5)["phase"] == "断流"


def test_parse_twist_line():
    line = '[2026-11-10 16:32:01.653] /cmd_vel (mission_engine(mroslaunch): geometry_msgs/Twist)[48]: {"linear":{"x":0.5,"y":0,"z":0},"angular":{"x":0,"y":0,"z":-0.2}}'
    assert runner.parse_twist(line) == (0.5, 0.0, -0.2)


def _wait(c, cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.05)
    assert cond()


def test_simulated_begin_end_produces_trials_and_report(monkeypatch):
    monkeypatch.setattr(runner, "PUSH_S", 1.0)
    monkeypatch.setattr(runner, "SETTLE_S", 0.5)
    c = LbmController()
    c.bind(robot="SIM", version="演练", simulated=True)
    _wait(c, lambda: c.prep["ok"])
    c.begin("前进", 2)
    with pytest.raises(ValueError):
        c.begin("前进", 2)
    _wait(c, lambda: len(c.take.counter.pushes) >= 2)
    live = c.state()["live"]
    assert live["reps"] == 2 and len(live["steps"]) == 7
    c.end()
    _wait(c, lambda: not c.finish["running"])
    f = c.state()["finish"]
    assert f["error"] == "" and f["result"]["found"] == 2
    assert all(st["state"] == "ok" for st in f["stages"])
    st = c.state()
    assert len(st["trials"]) == 2 and st["takes"][0]["n_found"] == 2
    md = Path(f["report"]).read_text(encoding="utf-8")
    assert "链路逐级" in md and "## 录包" in md
    r = subprocess.run([sys.executable, str(TOOLS / "lint_report_style.py"), "--file", f["report"]], capture_output=True, text=True)
    assert "错误 0" in r.stdout, r.stdout
    c.close_finish()
    assert c.state()["finish"] is None

    c.exclude(st["trials"][0]["id"], True, "推杆中途松手")
    assert c.state()["summary"][0]["n"] == 1


def test_discard_in_simulation_leaves_nothing():
    c = LbmController()
    c.bind(robot="SIM", version="演练", simulated=True)
    _wait(c, lambda: c.prep["ok"])
    c.begin("左转", 3)
    c.discard()
    assert c.take is None and c.state()["takes"] == []


def test_begin_refuses_until_robot_ready():
    c = LbmController(ssh_factory=lambda: (_ for _ in ()).throw(OSError("no route")))
    c.bind(robot="", version="", simulated=False)
    _wait(c, lambda: not c.prep["running"])
    assert not c.prep["ok"] and "热点" in c.prep["checks"][0]["detail"]
    with pytest.raises(ValueError, match="没准备好"):
        c.begin("前进")


class _Chan:
    def __init__(self, code):
        self.code = code

    def recv_exit_status(self):
        return self.code


class _Out:
    def __init__(self, text, code=0):
        self.text, self.channel = text, _Chan(code)

    def read(self):
        return self.text.encode()


class FakeRobot:
    """假主控：记录执行的命令和上传的文件。"""

    def __init__(self, have: dict[str, str]):
        self.have, self.cmds, self.put = have, [], []

    def exec_command(self, cmd, timeout=None, get_pty=False):
        self.cmds.append(cmd)
        if "md5sum analyze.py" in cmd:
            out = "".join(f"{v}  {k}\n" for k, v in self.have.items())
        elif cmd == "echo $HOME":
            out = "/home/limx\n"
        elif "which mrosrecord" in cmd:
            out = "/opt/limx/install/bin/mrosrecord\n180G\n"
        else:
            out = ""
        return None, _Out(out), _Out("")

    def open_sftp(self):
        robot = self

        class S:
            def put(self, src, dst):
                robot.put.append(dst)

            def close(self):
                pass

        return S()

    def close(self):
        pass


def test_new_robot_gets_tools_uploaded_and_up_to_date_robot_does_not():
    fresh = FakeRobot({})
    c = LbmController(ssh_factory=lambda: fresh)
    c.bind(robot="HU_D04_01_999", version="v", simulated=False, battery="80%")
    _wait(c, lambda: not c.prep["running"])
    assert c.prep["ok"], c.prep
    assert sorted(p.rsplit("/", 1)[1] for p in fresh.put) == ["analyze.py", "leg_odom.py", "rosbag_lite.py"]
    assert all(p.startswith("/home/limx/Cyres/lbm/tools/lbmtools/") for p in fresh.put)
    by = {x["name"]: x for x in c.prep["checks"]}
    assert "已更新 3" in by["分析脚本"]["detail"] and "180" in by["磁盘空间"]["detail"]

    same = FakeRobot(local_tool_md5())
    c2 = LbmController(ssh_factory=lambda: same)
    c2.bind(robot="HU_D04_01_998", version="v", simulated=False)
    _wait(c2, lambda: not c2.prep["running"])
    assert same.put == [] and c2.prep["ok"]


def test_nmcli_split_handles_escaped_colons():
    assert wifi._split(r"*:HU_D04_01_060_2.4G:72") == ["*", "HU_D04_01_060_2.4G", "72"]
    assert wifi._split(r"a\:b:c") == ["a:b", "c"]


def test_wifi_refuses_non_robot_ssid():
    with pytest.raises(ValueError):
        wifi.connect("LimX-vpn")


def test_bag_extract_tf_keep_only_matching_frames():
    import re
    import struct
    from bench.lbm.bag_extract import _tf_filter

    def tr(parent, child, x):
        s = lambda t: struct.pack("<I", len(t)) + t.encode()
        return struct.pack("<III", 1, 2, 3) + s(parent) + s(child) + struct.pack("<7d", x, 0, 0, 0, 0, 0, 1)

    msg = struct.pack("<I", 3) + tr("/world_limx", "/base_link", 1) + tr("pico_origin", "/vr_body/Pelvis", 2) \
        + tr("/world", "/Pelvis", 3)
    out = _tf_filter(msg, re.compile(r"^(pico_|/vr_body/)"))
    assert out == struct.pack("<I", 1) + tr("pico_origin", "/vr_body/Pelvis", 2)
    assert _tf_filter(msg, re.compile(r"^/nothing")) is None


def test_auto_script_bag_matches_timeline(tmp_path):
    """自动推杆脚本：先 RG+A、再按住握把，按动作表推满、松开，最后 RG+B；seq 连续。"""
    from bench.lbm import rosbag_lite as rl
    from bench.lbm import script as sc
    total = sc.make("左右转向", 2, tmp_path / "s.bag")
    rows = [(t, m) for _, t, m in rl.Bag(str(tmp_path / "s.bag")).read(["/vr_cmd"])]
    t0 = rows[0][0]

    def at(s):
        return min(rows, key=lambda r: abs(r[0] - t0 - s))[1]

    assert at(0.8)["A"] and at(0.8)["RG"] and not at(0.8)["LG"]
    assert at(4.0)["LG"] and at(4.0)["RG"] and not at(4.0)["A"]
    ps = sc.pushes("左右转向", 2)
    assert [p["kind"] for p in ps] == ["左转", "右转", "左转", "右转"]
    assert at(ps[0]["start"] + 1)["rightJS"][0] == -1.0 and at(ps[1]["start"] + 1)["rightJS"][0] == 1.0
    assert at(ps[0]["end"] + 1)["rightJS"] == [0.0, 0.0] and at(ps[0]["end"] + 1)["LG"]
    assert at(total - 0.6)["B"] and not any(at(total - 0.1)[k] for k in ("LG", "RG", "A", "B"))
    seqs = [m["header"]["seq"] for _, m in rows]
    assert all(b - a == 1 for a, b in zip(seqs, seqs[1:]))
    assert rows[-1][0] - t0 == pytest.approx(total, abs=0.05)


def test_auto_run_simulated_writes_both_directions():
    c = LbmController()
    c.bind(robot="SIM", version="0", simulated=True)
    for _ in range(50):
        if c.prep.get("ok"):
            break
        time.sleep(0.1)
    c.auto_begin("前进后退", 2)
    for _ in range(200):
        if not c.finish["running"]:
            break
        time.sleep(0.1)
    assert not c.finish["error"]
    assert sorted(t["item"] for t in c.trials()) == ["前进", "前进", "后退", "后退"]
    assert all(t.get("auto") for t in c.trials())
