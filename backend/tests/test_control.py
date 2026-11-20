"""状态切换：阻尼要解锁、吊装保护下不能坐躺、起身失败不再发 Walk、已在 Walk 算成功。"""
import asyncio

import pytest

from bench import control


class Fake:
    def __init__(self, replies):
        self.replies, self.sent = replies, []

    async def request(self, title, data=None, timeout=30):
        self.sent.append((title, data))
        return self.replies.get(title, {"result": "success"})


def run(robot, key, **kw):
    return asyncio.run(control.run(robot, key, **{"hanging": False, "unlocked": False, **kw}))


def test_damping_needs_unlock_and_hanging_blocks_sit():
    with pytest.raises(ValueError):
        run(Fake({}), "damping")
    assert run(Fake({}), "damping", unlocked=True)["ok"]
    with pytest.raises(ValueError):
        run(Fake({}), "sit", hanging=True)
    with pytest.raises(ValueError):
        run(Fake({}), "stand_hang")


def test_standup_chains_walk_and_stops_on_failure():
    r = Fake({})
    assert run(r, "stand_lie")["ok"]
    assert r.sent == [("request_standup", {"mode": "lying"}), ("request_set_walk_mode", {})]
    r = Fake({"request_standup": {"result": "fail_state_not_allowed", "current_state": "Damping"}})
    res = run(r, "stand_sit")
    assert not res["ok"] and "Damping" in res["text"] and len(r.sent) == 1


def test_walk_already_walking_counts_as_success():
    r = Fake({"request_set_walk_mode": {"result": "fail_state_not_allowed", "current_state": "Walk"}})
    assert run(r, "walk")["ok"]


def test_control_api_simulated(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEOP_BENCH_HOME", str(tmp_path))
    from fastapi.testclient import TestClient
    from bench import app as appmod
    c = TestClient(appmod.app)
    c.post("/api/mode", json={"simulated": True})
    from bench.robot import SimRobot
    monkeypatch.setattr(appmod, "SimRobot", lambda: SimRobot(fail_rate=0.0))  # 演练不随机失败
    assert c.post("/api/control", json={"action": "stand_hang", "hanging": True}).json()["ok"]
    r = c.post("/api/control", json={"action": "sit", "hanging": True})
    assert r.status_code == 400 and "吊装保护" in r.json()["detail"]
