"""验收测试：通过 HTTP 接口把每个页面的主流程端到端跑一遍，外部命令（录像脚本、分析脚本、ffmpeg）都是真的，
只有相机用假 ffmpeg 生成的合成录像代替。交付前必须全部通过。

运行：cd backend && python -m pytest -q tests/test_acceptance.py
"""
import os
import stat
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bench import config

FAKE = Path(__file__).parent / "fake"
import imageio_ffmpeg  # noqa: E402
import sys  # noqa: E402

REAL_FF = Path(imageio_ffmpeg.get_ffmpeg_exe())
VIS_PY = Path(sys.executable)

pytestmark = pytest.mark.skipif(not (REAL_FF.exists() and VIS_PY.exists()), reason="需要 imageio-ffmpeg 和 numpy")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEOP_BENCH_HOME", str(tmp_path))
    monkeypatch.setenv("REAL_FFMPEG", str(REAL_FF))
    monkeypatch.setenv("FAKE_PY", str(VIS_PY))
    monkeypatch.setenv("FAKE_T0", str(time.time()))
    (FAKE / "ffmpeg").chmod((FAKE / "ffmpeg").stat().st_mode | stat.S_IXUSR)
    config.save({"ffmpeg": str(FAKE / "ffmpeg"), "python": str(VIS_PY)})
    from bench.app import app, state
    state["simulated"] = False
    with TestClient(app) as c:
        yield c
    state["simulated"] = False


def _wait_latency(c, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = c.get("/api/latency").json()["job"]
        if job["phase"] in ("done", "failed"):
            return job
        time.sleep(0.5)
    raise AssertionError("录像分析超时")


def test_latency_calibrate_then_measure(client, monkeypatch):
    # 校准：两台都拍屏幕，B 没有延迟
    r = client.post("/api/latency/start", json={"kind": "calib", "name": "calib_t", "seconds": 12,
                                                "cam_a": "/dev/fakeA", "cam_b": "/dev/fakeB"})
    assert r.status_code == 200, r.text
    job = _wait_latency(client)
    assert job["phase"] == "done", job["error"] or job["result"].get("raw")
    calib = client.get("/api/latency").json()["calib_ms"]
    assert abs(calib) < 6, calib

    # 测量：B 透过“镜片”，比 A 晚 237 ms
    monkeypatch.setenv("FAKE_DELAY_fakeB", "0.237")
    r = client.post("/api/latency/start", json={"kind": "measure", "name": "tx1", "seconds": 12,
                                                "cam_a": "/dev/fakeA", "cam_b": "/dev/fakeB", "link": "近场 H264"})
    assert r.status_code == 200, r.text
    job = _wait_latency(client)
    assert job["phase"] == "done", job["error"] or job["result"].get("raw")
    res = job["result"]
    assert abs(res["mean"] - 237) < 8, res
    assert res["pairs"] >= 15 and "warning" not in res
    runs = client.get("/api/latency").json()["runs"]
    assert [x["kind"] for x in runs] == ["measure", "calib"]
    assert runs[0]["link"] == "近场 H264" and runs[0]["calib_ms"] == pytest.approx(calib, abs=0.01)

    # 删除无效结果
    assert client.delete(f"/api/latency/runs/{runs[0]['id']}").status_code == 200
    assert len(client.get("/api/latency").json()["runs"]) == 1


def test_latency_reports_missing_camera(client):
    r = client.post("/api/latency/start", json={"kind": "measure", "name": "tx1", "seconds": 12,
                                                "cam_a": "/dev/fakeA", "cam_b": "/dev/fakeB"})
    assert r.status_code == 400 and "校准" in r.json()["detail"]
    r = client.post("/api/latency/start", json={"kind": "calib", "name": "c1", "seconds": 12,
                                                "cam_a": "/dev/fakeA", "cam_b": "/dev/fakeA"})
    assert r.status_code == 400 and "两台不同的相机" in r.json()["detail"]


def test_stress_rehearsal_end_to_end(client):
    assert client.post("/api/mode", json={"simulated": True}).status_code == 200
    r = client.post("/api/stress/start", json={"sequence": "engine", "cycles": 3, "settle_s": 0, "hanging": True,
                                               "site_confirmed": False, "continue_on_fail": True})
    assert r.status_code == 200, r.text
    deadline = time.time() + 30
    while client.get("/api/stress").json()["running"] and time.time() < deadline:
        time.sleep(0.2)
    s = client.get("/api/stress").json()
    assert not s["running"] and s["end_reason"] == "全部完成"
    assert sum(x["total"] for x in s["summary"]) == 6
    assert client.get("/api/records").json() == []  # 演练不写台账
    r = client.post("/api/stress/start", json={"sequence": "sit", "cycles": 1, "hanging": True})
    assert r.status_code == 400 and "吊装" in r.json()["detail"]


def test_watch_rehearsal_end_to_end(client):
    client.post("/api/mode", json={"simulated": True})
    assert client.post("/api/watch/start", json={"scene": "持续站立稳定性", "use_imu": False}).status_code == 200
    time.sleep(1.2)
    s = client.post("/api/watch/inject").json()
    assert s["pending"] == 1
    assert client.post("/api/watch/finish", json={}).status_code == 400  # 有待确认事件不能结束
    client.post("/api/watch/judge", json={"event_id": 1, "verdict": "摔倒"})
    s = client.post("/api/watch/finish", json={"note": "验收"}).json()
    assert s["state"] == "finished" and s["falls"] == 1 and s["elapsed_s"] >= 1


def test_ledger_end_to_end(client):
    r = client.post("/api/records", json={"scene": "Move 稳定性", "issue_type": "摔倒", "metric": "CPI-F",
                                          "total": 136, "events": 0, "target": 100})
    assert r.status_code == 200 and r.json()["display"] == "136+" and r.json()["meets_target"] is True
    assert client.post("/api/records", json={"scene": "x", "issue_type": "y", "metric": "CPI", "total": 2.5,
                                             "events": 1}).status_code == 400
    md = client.get("/api/records/markdown").text
    assert "Move 稳定性（摔倒），CPI-F = 136+（136/0）" in md


def test_frontend_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "遥操测试台" in r.text
    assert client.get("/api/docs").text.startswith("# 遥操测试台使用说明")


def test_cam_capture_script_exits_cleanly(tmp_path, monkeypatch):
    """录像脚本本身要正常退出并打印帧数（11-09 曾因 ffmpeg 7 复制流不打印 frame= 而误报失败）。"""
    import subprocess
    env = dict(os.environ, FFMPEG=str(FAKE / "ffmpeg"), REAL_FFMPEG=str(REAL_FF), FAKE_PY=str(VIS_PY),
               FAKE_T0=str(time.time()))
    tools = Path(__file__).resolve().parents[1] / "tools"
    p = subprocess.run(["bash", str(tools / "cam_capture.sh"), "x", "5", "2560x720", "/dev/fakeA", "/dev/fakeB"],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "x_A.mkv：150 帧" in p.stdout and "x_B.mkv：300 帧" in p.stdout, p.stdout


def test_latency_report_build_and_publish(client, monkeypatch):
    """测量完一键生成报告：图、视频、表格齐全，写作规范 0 错误，飞书能解析，发布拿到链接。"""
    import subprocess
    for kind, name, delay, link in (("calib", "calib_r", "0", ""), ("measure", "tx1", "0.083", "近场 H264"),
                                    ("measure", "tx2", "0.181", "远程 阿里云 H264")):
        monkeypatch.setenv("FAKE_DELAY_fakeB", delay)
        r = client.post("/api/latency/start", json={"kind": kind, "name": name, "seconds": 12, "cam_a": "/dev/fakeA",
                                                    "cam_b": "/dev/fakeB", "link": link})
        assert r.status_code == 200, r.text
        assert _wait_latency(client)["phase"] == "done"
    r = client.post("/api/report/latency", json={"basis": "验收测试", "note": ""})
    assert r.status_code == 200, r.text
    meta = r.json()
    d = Path(meta["dir"])
    assert meta["lint"]["errors"] == 0, meta["lint"]["output"]
    xml = (d / "report.xml").read_text(encoding="utf-8")
    for must in ("近场 H264", "远程 阿里云 H264", "测试准备", "对比汇总", "逐项证据", "脚本与工具", "结论", "超出目标"):
        assert must in xml, must
    for f in ("fig/对比.png", "fig/tx1_2_分布.png", "fig/tx1_2_画面.png", "video/tx1_2_A屏幕_B镜片.mp4", "tools/flash_latency.py"):
        assert (d / f).stat().st_size > 1000, f
    lark = Path.home() / ".local/bin/lark-cli"
    if lark.exists():
        p = subprocess.run([str(lark), "docs", "+script", "--command", "parse", "--content", "@report.xml"],
                           cwd=d, capture_output=True, text=True, timeout=120)
        assert '"ok": true' in p.stdout, p.stdout[-500:] + p.stderr[-500:]
    config.save({"lark_cli": str(FAKE / "lark-cli")})
    r = client.post("/api/report/publish", json={"dir": str(d)})
    assert r.status_code == 200, r.text
    assert r.json()["url"].endswith("FAKEDOC")
    assert client.post("/api/report/publish", json={"dir": "/tmp"}).status_code == 400


def test_latency_same_name_twice_keeps_both_recordings(client):
    """同名测两次不能互相覆盖（11-09 真机上 4 次都叫 tx1，录像被后一次覆盖）。"""
    client.post("/api/latency/start", json={"kind": "calib", "name": "c1", "seconds": 12, "cam_a": "/dev/fakeA",
                                            "cam_b": "/dev/fakeB"})
    assert _wait_latency(client)["phase"] == "done"
    for _ in range(2):
        time.sleep(1.1)  # 文件名带时分秒
        client.post("/api/latency/start", json={"kind": "measure", "name": "tx1", "seconds": 12, "cam_a": "/dev/fakeA",
                                                "cam_b": "/dev/fakeB", "link": "近场 H264"})
        assert _wait_latency(client)["phase"] == "done"
    runs = [r for r in client.get("/api/latency").json()["runs"] if r["kind"] == "measure"]
    files = {f for r in runs for f in r["result"]["files"]} | {r["result"]["csv"] for r in runs}
    assert len(files) == 6 and all(Path(f).exists() for f in files)


def test_latency_camera_dropout_is_reported(client):
    """相机 B 录像途中掉线：要报录像失败并指出是哪台，不能拿旧文件去分析。"""
    client.post("/api/latency/start", json={"kind": "calib", "name": "c1", "seconds": 12, "cam_a": "/dev/fakeA",
                                            "cam_b": "/dev/fakeB"})
    assert _wait_latency(client)["phase"] == "done"
    client.post("/api/latency/start", json={"kind": "measure", "name": "tx1", "seconds": 12, "cam_a": "/dev/fakeA",
                                            "cam_b": "/dev/fakeDeadB", "link": "近场 H264"})
    job = _wait_latency(client)
    assert job["phase"] == "failed" and "录像失败" in job["error"] and "相机 B" in job["error"], job
    assert "No such device" in job["result"]["raw"]
    r = client.post("/api/latency/start", json={"kind": "measure", "name": "tx1", "seconds": 12, "cam_a": "/dev/fakeA",
                                                "cam_b": "/dev/video99", "link": "近场 H264"})
    assert r.status_code == 400 and "相机 B（/dev/video99） 不在了" in r.json()["detail"]


def test_watch_sway_and_ts_rehearsal(client):
    """演练：合成走 6 s 停 4 s，摆幅有读数；在走的时候点“松指令”，停下后测出 Ts。"""
    client.post("/api/mode", json={"simulated": True})
    assert client.post("/api/watch/start", json={"scene": "VR全身动捕持续跟随", "use_imu": True}).status_code == 200
    deadline = time.time() + 30
    s = client.get("/api/watch").json()
    while time.time() < deadline:
        s = client.get("/api/watch").json()
        if s["sway"]["n"] >= 2 and s["joint_rate"] > 0:
            break
        time.sleep(0.5)
    assert s["sway"]["pass"] is True and s["sway"]["pitch"]["median"] > 1, s["sway"]
    r = client.post("/api/watch/release")
    assert r.status_code == 200, r.text
    while time.time() < deadline and not client.get("/api/watch").json()["ts"]["n"]:
        time.sleep(0.3)
    ts = client.get("/api/watch").json()["ts"]
    assert ts["stable"] == 1 and 0 <= ts["median"] <= 7, ts
    s = client.post("/api/watch/finish", json={}).json()
    assert s["state"] == "finished" and s["saved_measure_ids"] == []  # 演练不写
    client.post("/api/mode", json={"simulated": False})


def test_stress_fall_rehearsal(client):
    client.post("/api/mode", json={"simulated": True})
    client.post("/api/stress/start", json={"sequence": "slm", "cycles": 40, "settle_s": 0, "hanging": False})
    time.sleep(0.5)
    s = client.post("/api/stress/fall", json={"detail": "侧摔"}).json()
    deadline = time.time() + 20
    while s["running"] and time.time() < deadline:
        time.sleep(0.2)
        s = client.get("/api/stress").json()
    assert s["end_reason"] == "摔倒，F1 一票否决" and s["f1"] and s["blocks"][-1]["falls"] == 1
    client.post("/api/mode", json={"simulated": False})


def test_boundary_page_flow_rehearsal(client):
    client.post("/api/mode", json={"simulated": True})
    st = client.get("/api/boundary").json()
    assert st["session"].startswith("演练") and len(st["actions"]) == 10
    assert client.post("/api/boundary/stream", json={"on": True}).json()["stream_on"]
    client.post("/api/boundary/begin", json={"action": "深蹲"})
    time.sleep(2.5)
    r = client.post("/api/boundary/result", json={"action": "深蹲", "result": "饱和"})
    assert r.status_code == 400 and "边界" in r.json()["detail"]
    st = client.post("/api/boundary/result", json={"action": "深蹲", "result": "饱和", "boundary": "蹲到大腿水平"}).json()
    assert st["trials"][-1]["sway"] != ""  # 读姿态时有摆幅峰值
    client.post("/api/boundary/controller", json={"controller": "LBMotion"})
    st = client.post("/api/boundary/result", json={"action": "深蹲", "result": "摔倒"}).json()
    rows = {r["controller"]: r for r in st["summary"]}
    assert rows["WBT"]["completion"] == 100 and rows["LBMotion"]["f1"]
    assert client.post("/api/boundary/save").status_code == 400  # 演练不写台账
    client.post("/api/boundary/stream", json={"on": False})
    client.post("/api/mode", json={"simulated": False})


def test_ledger_rules_over_http(client):
    for v, ev in (("v1", 1), ("v2", 3)):
        client.post("/api/records", json={"scene": "平地行走", "issue_type": "摔倒", "metric": "CPI-F", "total": 30,
                                          "events": ev, "version": v, "layer": "L2"})
    recs = client.get("/api/records").json()
    assert all(r["status"] == "f1" for r in recs) and recs[0]["prev_display"]
    assert client.post("/api/records", json={"scene": "x", "issue_type": "y", "metric": "CPI", "total": 3, "events": 0,
                                             "layer": "L9"}).status_code == 400
    cad = client.get("/api/cadence").json()
    assert [l["layer"] for l in cad["layers"]] == ["L1", "L2", "L3", "L4", "L5"]
    assert client.get("/api/measures").json() == []
