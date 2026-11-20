"""夹爪夹持力：默认、0、100 三档，每档没传照片不能进下一档，测完恢复默认值。"""
import base64
import time

import pytest

from bench.gripper import GripperController, plan


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEOP_BENCH_HOME", str(tmp_path))


def _wait(g):
    for _ in range(100):
        if not g.busy:
            return
        time.sleep(0.02)


def test_plan_skips_duplicate_levels():
    assert plan(80) == [80, 0, 100]
    assert plan(100) == [100, 0]


def test_three_levels_need_photo_and_restore_default():
    g = GripperController()
    g.bind(robot="SIM", version="0", simulated=True)
    g.start("两侧")
    _wait(g)
    png = base64.b64encode(b"\x89PNG fake").decode()
    seen = []
    for _ in range(3):
        st = g.t["steps"][-1]
        seen.append((st["force"], st["readback"]))
        with pytest.raises(ValueError):
            g.finish_step("")
        g.add_photo("a.png", png)
        g.finish_step("凹进 3 mm")
        _wait(g)
    assert seen == [(80, {"left": 80, "right": 80}), (0, {"left": 0, "right": 0}), (100, {"left": 100, "right": 100})]
    assert g.t["done"] and g.t["restored"] == {"left": 80, "right": 80}
    assert not g.error
    report = (g._dir() / "报告.md").read_text(encoding="utf-8")
    assert report.count("![") == 3 and "凹进 3 mm" in report
