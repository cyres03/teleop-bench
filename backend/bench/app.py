"""遥操测试台后端：FastAPI，同时托管前端页面。

开发：uvicorn bench.app:app --reload --port 8765（在 backend 目录）
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, report, robotcfg, wifi
from .latency import LatencyController, list_cameras, open_flash_page, snapshot
from .robot import Identity, RealRobot, SimRobot, read_identity
from .stress import StressController, sequences_public
from .boundary import BoundaryController
from .watch import WatchController
from .lbm.runner import LbmController
from .wbt.runner import WbtController
from .gripper import GripperController
from . import control
from . import access

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "frontend" / "dist"
DOCS = ROOT / "docs" / "使用说明.md"

app = FastAPI(title="遥操测试台")
state = {"simulated": False, "identity": Identity()}
stress = StressController()
watch = WatchController(ssh_factory=config.ssh_client)
latency = LatencyController()
boundary = BoundaryController(ssh_factory=config.ssh_client)
lbm = LbmController(ssh_factory=config.ssh_client)
wbt = WbtController(ssh_factory=config.ssh_client)
gripper = GripperController(ssh_factory=config.ssh_client)


@app.on_event("startup")
async def _startup() -> None:
    db.init()


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


# ---------- 机器人 ----------
class ModeIn(BaseModel):
    simulated: bool


@app.get("/api/robot")
async def robot_status():
    if state["simulated"]:
        ident = Identity(sn=SimRobot.accid, version="演练", robot_status="Walk", battery="—", reachable=True,
                         simulated=True)
    else:
        ident = await read_identity()
    state["identity"] = ident
    return ident.as_dict()


# ---------- 第一次连机器人：软件里输密码授权 ----------
class AccessIn(BaseModel):
    limx: str
    guest: str = ""


_access_cache: dict[str, dict] = {}


@app.get("/api/robot/access")
async def robot_access():
    if state["simulated"]:
        return {"sn": "", "main": True, "video": True}
    sn = state["identity"].sn
    if not sn:
        return {"sn": "", "main": True, "video": True}
    if sn not in _access_cache:
        _access_cache[sn] = await asyncio.to_thread(access.status)
    return {"sn": sn, **_access_cache[sn]}


@app.post("/api/robot/access")
async def robot_access_grant(body: AccessIn):
    cfg = config.load()
    sn = state["identity"].sn
    try:
        await asyncio.to_thread(access.authorize, cfg["ssh_host"], cfg["ssh_user"], body.limx)
        video = ""
        if body.guest:
            try:
                await asyncio.to_thread(access.authorize, cfg["video_ssh_host"], cfg["video_ssh_user"], body.guest)
            except ValueError as exc:
                video = f"；1.3 没授权成功（{exc}），只影响视频延迟页"
    except Exception as exc:
        raise _bad(exc)
    _access_cache.pop(sn, None)
    lbm.prepare(force=True)
    wbt.prepare(force=True)
    return {"ok": True, "text": "授权好了，这台机器人以后不用再输密码" + video}


@app.post("/api/mode")
async def set_mode(body: ModeIn):
    if stress.run.running or watch.session.state in ("running", "paused"):
        raise HTTPException(400, "压测或看护进行中，结束后再切换模式")
    state["simulated"] = body.simulated
    return {"simulated": body.simulated}


def _robot():
    if state["simulated"]:
        return SimRobot()
    ident: Identity = state["identity"]
    return RealRobot(ident.sn)


# ---------- 台账 ----------
class RecordIn(BaseModel):
    scene: str
    issue_type: str
    metric: str
    total: float
    events: int
    target: float | None = None
    product: str = "Oli"
    version: str = ""
    robot: str = ""
    note: str = ""
    evidence: str = ""
    layer: str = ""
    flat: bool = True


@app.get("/api/records")
def records():
    return db.list_records()


@app.post("/api/records")
def add_record(body: RecordIn):
    try:
        return db.add_record(**body.model_dump(), source="手动")
    except ValueError as exc:
        raise _bad(exc)


@app.delete("/api/records/{record_id}")
def delete_record(record_id: int):
    if not db.delete_record(record_id):
        raise HTTPException(404, "没有这条记录")
    return {"ok": True}


@app.get("/api/records/markdown", response_class=PlainTextResponse)
def records_markdown():
    return db.ledger_markdown(db.list_records())


@app.get("/api/measures")
def measures():
    return db.list_measures()


@app.delete("/api/measures/{mid}")
def delete_measure(mid: int):
    if not db.delete_measure(mid):
        raise HTTPException(404, "没有这条测量")
    return {"ok": True}


@app.get("/api/cadence")
def cadence():
    """当前版本：连着真机用机器人的固件版本，否则用台账里最新的版本。"""
    ident: Identity = state["identity"]
    live = not state["simulated"] and not ident.simulated and ident.sn and ident.version
    version, source = (ident.version, "机器人") if live else (db.latest_version(), "台账最新")
    return {"version": version, "source": source, "layers": db.cadence(version)}


# ---------- 模式切换压测 ----------
class StressIn(BaseModel):
    sequence: str
    cycles: int
    settle_s: float | None = None
    hanging: bool = True
    site_confirmed: bool = False
    continue_on_fail: bool = False


@app.get("/api/stress/sequences")
def stress_sequences():
    return sequences_public()


@app.get("/api/stress")
def stress_state():
    return stress.run.as_dict()


@app.post("/api/stress/start")
async def stress_start(body: StressIn):
    try:
        robot = _robot()
        run = stress.start(robot, version=state["identity"].version, **body.model_dump())
    except Exception as exc:
        raise _bad(exc)
    return run.as_dict()


@app.post("/api/stress/stop")
def stress_stop():
    stress.stop()
    return stress.run.as_dict()


class FallIn(BaseModel):
    detail: str = ""


@app.post("/api/stress/fall")
def stress_fall(body: FallIn):
    try:
        stress.record_fall(body.detail)
    except ValueError as exc:
        raise _bad(exc)
    return stress.run.as_dict()


# ---------- L4 边界探测 ----------
class BeginIn(BaseModel):
    action: str


class TrialIn(BaseModel):
    action: str
    result: str
    boundary: str = ""
    note: str = ""


class ControllerIn(BaseModel):
    controller: str


class StreamIn(BaseModel):
    on: bool


def _boundary():
    ident: Identity = state["identity"]
    boundary.bind(robot=ident.sn, version=ident.version, simulated=state["simulated"])
    return boundary


@app.get("/api/boundary")
def boundary_state():
    return _boundary().state()


@app.post("/api/boundary/controller")
def boundary_controller(body: ControllerIn):
    try:
        _boundary().set_controller(body.controller)
    except ValueError as exc:
        raise _bad(exc)
    return boundary.state()


@app.post("/api/boundary/stream")
def boundary_stream(body: StreamIn):
    b = _boundary()
    if body.on and not state["simulated"] and not state["identity"].sn:
        raise HTTPException(400, "没有识别到机器人，先连上机器人热点")
    try:
        b.stream(body.on)
    except Exception as exc:
        raise _bad(exc)
    return b.state()


@app.post("/api/boundary/begin")
def boundary_begin(body: BeginIn):
    try:
        _boundary().begin(body.action)
    except ValueError as exc:
        raise _bad(exc)
    return boundary.state()


@app.post("/api/boundary/result")
def boundary_result(body: TrialIn):
    try:
        _boundary().result(**body.model_dump())
    except ValueError as exc:
        raise _bad(exc)
    return boundary.state()


@app.delete("/api/boundary/trials/{tid}")
def boundary_delete(tid: int):
    if not db.delete_trial(tid):
        raise HTTPException(404, "没有这次记录")
    return _boundary().state()


@app.post("/api/boundary/save")
def boundary_save():
    try:
        _boundary().save_to_ledger()
    except ValueError as exc:
        raise _bad(exc)
    return boundary.state()


# ---------- LBMotion 推杆测试 ----------
class LbmBeginIn(BaseModel):
    item: str
    reps: int = 5


class LbmAnalyzeIn(BaseModel):
    path: str


class LbmExcludeIn(BaseModel):
    id: int
    excluded: bool
    note: str = ""


class WifiIn(BaseModel):
    ssid: str
    password: str = ""


def _lbm():
    ident: Identity = state["identity"]
    lbm.bind(robot=ident.sn, version=ident.version, simulated=state["simulated"], battery=ident.battery, status=ident.robot_status)
    return lbm


@app.get("/api/lbm")
def lbm_state():
    return _lbm().state()


@app.post("/api/lbm/prepare")
async def lbm_prepare():
    if not state["simulated"]:
        state["identity"] = await read_identity()
    _lbm().prepare(force=True)
    return lbm.state()


@app.post("/api/lbm/begin")
async def lbm_begin(body: LbmBeginIn):
    try:
        await asyncio.to_thread(_lbm().begin, body.item, body.reps)
    except Exception as exc:
        raise _bad(exc)
    return lbm.state()


class LbmAutoIn(BaseModel):
    script: str
    reps: int = 3


@app.post("/api/lbm/auto")
def lbm_auto(body: LbmAutoIn):
    try:
        _lbm().auto_begin(body.script, body.reps)
    except Exception as exc:
        raise _bad(exc)
    return lbm.state()


@app.post("/api/lbm/auto/stop")
def lbm_auto_stop():
    try:
        _lbm().auto_stop()
    except ValueError as exc:
        raise _bad(exc)
    return lbm.state()


@app.post("/api/lbm/end")
def lbm_end():
    try:
        _lbm().end()
    except ValueError as exc:
        raise _bad(exc)
    return lbm.state()


@app.post("/api/lbm/discard")
async def lbm_discard():
    try:
        await asyncio.to_thread(_lbm().discard)
    except Exception as exc:
        raise _bad(exc)
    return lbm.state()


@app.post("/api/lbm/close")
def lbm_close():
    _lbm().close_finish()
    return lbm.state()


@app.post("/api/lbm/analyze")
async def lbm_analyze(body: LbmAnalyzeIn):
    try:
        await asyncio.to_thread(_lbm().analyze_local, body.path)
    except Exception as exc:
        raise _bad(exc)
    return lbm.state()


@app.post("/api/lbm/exclude")
def lbm_exclude(body: LbmExcludeIn):
    if body.excluded and not body.note.strip():
        raise HTTPException(400, "排除要写原因（例如推杆中途松手、被人扶）")
    _lbm().exclude(body.id, body.excluded, body.note)
    _lbm().write_report()
    return lbm.state()


@app.delete("/api/lbm/trials/{tid}")
def lbm_delete(tid: int):
    if not _lbm().delete(tid):
        raise HTTPException(404, "没有这次记录")
    lbm.write_report()
    return lbm.state()


@app.post("/api/lbm/fetch")
def lbm_fetch():
    try:
        _lbm().fetch_bags()
    except ValueError as exc:
        raise _bad(exc)
    return lbm.state()


@app.post("/api/lbm/open")
def lbm_open():
    return {"dir": _lbm().open_folder()}


@app.get("/api/lbm/markdown", response_class=PlainTextResponse)
def lbm_markdown():
    return _lbm().markdown()


# ---------- 全身遥操回放 ----------
class WbtBeginIn(BaseModel):
    item: str


class WbtAnalyzeIn(BaseModel):
    path: str
    item: str = ""


def _wbt():
    ident: Identity = state["identity"]
    wbt.bind(robot=ident.sn, version=ident.version, simulated=state["simulated"], battery=ident.battery)
    return wbt


@app.get("/api/wbt")
def wbt_state():
    return _wbt().state()


@app.post("/api/wbt/prepare")
async def wbt_prepare():
    if not state["simulated"]:
        state["identity"] = await read_identity()
    _wbt().prepare(force=True)
    return wbt.state()


@app.post("/api/wbt/begin")
def wbt_begin(body: WbtBeginIn):
    try:
        _wbt().begin(body.item)
    except Exception as exc:
        raise _bad(exc)
    return wbt.state()


@app.post("/api/wbt/stop")
def wbt_stop():
    try:
        _wbt().stop()
    except ValueError as exc:
        raise _bad(exc)
    return wbt.state()


@app.post("/api/wbt/analyze")
async def wbt_analyze(body: WbtAnalyzeIn):
    try:
        await asyncio.to_thread(_wbt().analyze_local, body.path, body.item)
    except Exception as exc:
        raise _bad(exc)
    return wbt.state()


@app.delete("/api/wbt/runs/{rid}")
def wbt_delete(rid: int):
    if not _wbt().delete(rid):
        raise HTTPException(404, "没有这次记录")
    return wbt.state()


@app.get("/api/wbt/img/{rid}")
def wbt_img(rid: int):
    p = _wbt().image(rid)
    if not p:
        raise HTTPException(404, "没有图")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "no-cache"})


@app.post("/api/wbt/open")
def wbt_open():
    return {"dir": _wbt().open_folder()}


# ---------- 夹爪夹持力 ----------
class GripStartIn(BaseModel):
    side: str = "两侧"


class GripPhotoIn(BaseModel):
    filename: str
    data: str


class GripNoteIn(BaseModel):
    note: str = ""


def _grip():
    ident: Identity = state["identity"]
    gripper.bind(robot=ident.sn, version=ident.version, simulated=state["simulated"])
    return gripper


def _grip_call(fn, *a):
    try:
        fn(*a)
    except Exception as exc:
        raise _bad(exc)
    return gripper.state()


@app.get("/api/gripper")
def grip_state():
    return _grip().state()


@app.post("/api/gripper/start")
def grip_start(body: GripStartIn):
    return _grip_call(_grip().start, body.side)


@app.post("/api/gripper/photo")
def grip_photo(body: GripPhotoIn):
    return _grip_call(_grip().add_photo, body.filename, body.data)


@app.delete("/api/gripper/photo/{name}")
def grip_photo_del(name: str):
    return _grip_call(_grip().remove_photo, name)


@app.get("/api/gripper/img/{name}")
def grip_img(name: str):
    p = _grip().photo(name)
    if not p:
        raise HTTPException(404, "没有这张照片")
    return FileResponse(p)


@app.post("/api/gripper/finish")
def grip_finish(body: GripNoteIn):
    return _grip_call(_grip().finish_step, body.note)


@app.post("/api/gripper/abort")
def grip_abort():
    return _grip_call(_grip().abort)


@app.post("/api/gripper/close")
def grip_close():
    return _grip_call(_grip().close)


@app.post("/api/gripper/open")
def grip_open():
    return {"dir": _grip().open_folder()}


# ---------- 机器人状态切换（阻尼、IKStand、Walk、坐下、躺下、起身） ----------
class ControlIn(BaseModel):
    action: str
    hanging: bool = True
    unlocked: bool = False


_control_busy = {"action": ""}


def _testing() -> str:
    if stress.run.running:
        return "模式切换压测"
    if lbm.take or (lbm.finish and lbm.finish.get("running")):
        return "LBMotion 推杆"
    if wbt.run and wbt.run.get("active"):
        return "全身遥操回放"
    return ""


@app.get("/api/control")
def control_actions():
    return {"actions": control.public(), "busy": _control_busy["action"], "testing": _testing()}


@app.post("/api/control")
async def control_run(body: ControlIn):
    if _control_busy["action"]:
        raise HTTPException(409, f"正在{_control_busy['action']}，等它做完")
    if (t := _testing()):
        raise HTTPException(409, f"{t}正在跑，测试会自己切状态，别手动切")
    try:
        robot = _robot()
        _control_busy["action"] = control.ACTIONS.get(body.action, {}).get("label", body.action)
        res = await control.run(robot, body.action, hanging=body.hanging, unlocked=body.unlocked)
    except Exception as exc:
        raise _bad(exc)
    finally:
        _control_busy["action"] = ""
    if not state["simulated"]:
        state["identity"] = await read_identity()
    return res


@app.get("/api/wifi")
async def wifi_list():
    try:
        return await asyncio.to_thread(wifi.hotspots)
    except Exception as exc:
        raise _bad(exc)


@app.post("/api/wifi/connect")
async def wifi_connect(body: WifiIn):
    try:
        await asyncio.to_thread(wifi.connect, body.ssid, body.password)
    except Exception as exc:
        raise _bad(exc)
    for _ in range(10):  # 等 DHCP 拿到地址、机器人端口能连上
        ident = await read_identity()
        if ident.sn:
            break
        await asyncio.sleep(1.5)
    state["identity"] = ident
    _lbm().prepare(force=True)
    return {"identity": ident.as_dict(), "lbm": lbm.state()}


# ---------- 长测看护 ----------
class WatchIn(BaseModel):
    scene: str
    use_imu: bool = False
    threshold_deg: float = 60.0
    flat: bool = True


class JudgeIn(BaseModel):
    event_id: int
    verdict: str


class ManualEventIn(BaseModel):
    detail: str
    verdict: str = "issue"


class FinishIn(BaseModel):
    note: str = ""


@app.get("/api/watch")
def watch_state():
    return watch.session.as_dict()


@app.post("/api/watch/start")
async def watch_start(body: WatchIn):
    ident: Identity = state["identity"]
    if not state["simulated"] and not ident.sn:
        raise HTTPException(400, "没有识别到机器人，先连上机器人热点")
    try:
        s = watch.start(scene=body.scene, simulated=state["simulated"], robot=ident.sn, version=ident.version,
                        use_imu=body.use_imu, threshold_deg=body.threshold_deg, flat=body.flat)
    except ValueError as exc:
        raise _bad(exc)
    return s.as_dict()


@app.post("/api/watch/pause")
def watch_pause():
    watch.pause()
    return watch.session.as_dict()


@app.post("/api/watch/resume")
def watch_resume():
    watch.resume()
    return watch.session.as_dict()


@app.post("/api/watch/judge")
def watch_judge(body: JudgeIn):
    try:
        watch.judge(body.event_id, body.verdict)
    except ValueError as exc:
        raise _bad(exc)
    return watch.session.as_dict()


@app.post("/api/watch/event")
def watch_event(body: ManualEventIn):
    if watch.session.state not in ("running", "paused"):
        raise HTTPException(400, "看护没有在进行")
    if body.verdict not in ("摔倒", "issue"):
        raise HTTPException(400, "手动记录只能是 摔倒 或 issue")
    watch.add_event("手动", body.detail or "现场记录", body.verdict)
    return watch.session.as_dict()


@app.post("/api/watch/release")
def watch_release():
    try:
        watch.mark_release()
    except ValueError as exc:
        raise _bad(exc)
    return watch.session.as_dict()


@app.post("/api/watch/inject")
def watch_inject():
    try:
        watch.inject_fall()
    except ValueError as exc:
        raise _bad(exc)
    return watch.session.as_dict()


@app.post("/api/watch/finish")
def watch_finish(body: FinishIn):
    try:
        return watch.finish(body.note).as_dict()
    except ValueError as exc:
        raise _bad(exc)


# ---------- 视频延迟 ----------
class LatencyIn(BaseModel):
    kind: str
    name: str
    seconds: int = 30
    cam_a: str
    cam_b: str
    link: str = ""
    note: str = ""
    skip_robot_check: bool = False


class FlashIn(BaseModel):
    monitor: int = 1


@app.get("/api/latency")
def latency_state():
    return {"job": latency.job.as_dict(), "runs": db.list_latency_runs(), "calib_ms": latency.latest_calib()}


@app.get("/api/latency/cameras")
def latency_cameras():
    return list_cameras()


@app.get("/api/latency/snapshot")
async def latency_snapshot(dev: str):
    if not dev.startswith("/dev/video"):
        raise HTTPException(400, "只能读 /dev/video*")
    try:
        data = await asyncio.to_thread(snapshot, dev, config.load()["video_size"])
    except Exception as exc:
        raise _bad(exc)
    return Response(content=data, media_type="image/jpeg")


@app.post("/api/latency/flash")
def latency_flash(body: FlashIn):
    open_flash_page(body.monitor)
    return {"ok": True}


@app.delete("/api/latency/runs/{run_id}")
def latency_delete(run_id: int):
    if not db.delete_latency_run(run_id):
        raise HTTPException(404, "没有这条结果")
    return {"ok": True}


def _split_link(link: str) -> tuple[str, str]:
    """“远程 腾讯云 H264” → （“远程 腾讯云”，“H264”）。"""
    parts = link.rsplit(" ", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (link, "")


async def _robot_video_note(link: str, skip: bool) -> str:
    """正式测量前读机器人 1.3 上的视频配置，和要测的链路、编码核对；结果记进这次测量的备注。"""
    path, codec = _split_link(link)
    try:
        st = await asyncio.to_thread(robotcfg.read_state)
    except Exception as exc:
        if skip:
            return "机器人视频配置未核实（读取失败，手动跳过核对）"
        raise ValueError(f"读不到机器人视频配置（{exc}）。确认电脑连着机器人热点；"
                         "确定要测就勾选“跳过核对”，这次结果会标“配置未核实”") from None
    why = robotcfg.check_match(st, path, codec)
    snap = (f"机器人配置（1.3 {time.strftime('%H:%M:%S')}）：近场 {str(st['local_codec']).upper()}，"
            f"远程 {st['remote_name']} {str(st['remote_codec'] or '').upper()}")
    if why:
        if skip:
            return f"{snap}；和要测的不一致（{why}），手动跳过核对"
        raise ValueError(f"{why}。先在上面切换，或把链路、编码改成和机器人一致")
    return snap


@app.post("/api/latency/start")
async def latency_start(body: LatencyIn):
    ident: Identity = state["identity"]
    args = body.model_dump()
    skip = args.pop("skip_robot_check")
    try:
        if body.kind == "measure" and not state["simulated"] and ident.sn:
            snap = await _robot_video_note(body.link, skip)
            args["note"] = "；".join(x for x in (args["note"], snap) if x)
        job = latency.start(robot=ident.sn, version=ident.version, **args)
    except ValueError as exc:
        raise _bad(exc)
    return job.as_dict()


# ---------- 报告 ----------
class ReportIn(BaseModel):
    ids: list[int] | None = None
    basis: str = ""
    note: str = ""


class PublishIn(BaseModel):
    dir: str


@app.post("/api/report/latency")
async def report_build(body: ReportIn):
    try:
        meta = await asyncio.to_thread(report.build, body.ids, basis=body.basis, note=body.note)
    except ValueError as exc:
        raise _bad(exc)
    meta["preview"] = report.preview_markdown(meta)
    return meta


@app.post("/api/report/publish")
async def report_publish(body: PublishIn):
    root = (db.data_dir() / "reports").resolve()
    target = Path(body.dir).resolve()
    if root not in target.parents:
        raise HTTPException(400, "只能发布本软件生成的报告")
    try:
        return await asyncio.to_thread(report.publish, str(target))
    except Exception as exc:
        raise _bad(exc)


class OpenIn(BaseModel):
    url: str


@app.post("/api/open")
def open_url(body: OpenIn):
    if not body.url.startswith(("https://", "http://")):
        raise HTTPException(400, "只能打开网页链接")
    import webbrowser
    webbrowser.open(body.url)
    return {"ok": True}


# ---------- 机器人视频配置 ----------
class CodecIn(BaseModel):
    codec: str
    confirmed: bool = False


class ConfirmIn(BaseModel):
    confirmed: bool = False


class RemoteIn(BaseModel):
    provider: str
    codec: str = "h264"
    confirmed: bool = False


class SudoIn(BaseModel):
    password: str


def _cfg_log(action: str, args: dict, ok: bool, detail: str) -> None:
    """机器人配置操作日志 ~/.teleop-bench/robotcfg.log（一行一条，不含密码）。"""
    import json as _json
    line = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action, "args": args, "ok": ok, "detail": detail[-600:]}
    with (db.data_dir() / "robotcfg.log").open("a", encoding="utf-8") as fh:
        fh.write(_json.dumps(line, ensure_ascii=False) + "\n")


async def _cfg_call(action: str, args: dict, fn, *a, **kw):
    try:
        res = await asyncio.to_thread(fn, *a, **kw)
    except Exception as exc:
        _cfg_log(action, args, False, f"{type(exc).__name__}: {exc}")
        raise _bad(exc)
    _cfg_log(action, args, True, res.get("message", "") if isinstance(res, dict) else "")
    return res


def _need_real_robot() -> str:
    if state["simulated"]:
        raise HTTPException(400, "演练模式不改机器人配置")
    sn = state["identity"].sn
    if not sn:
        raise HTTPException(400, "没有识别到机器人")
    return sn


@app.get("/api/robotcfg")
async def robotcfg_state():
    _need_real_robot()
    try:
        return await asyncio.to_thread(robotcfg.read_state)
    except Exception as exc:
        raise _bad(exc)


@app.post("/api/robotcfg/codec")
async def robotcfg_codec(body: CodecIn):
    sn = _need_real_robot()
    if not body.confirmed:
        raise HTTPException(400, "改配置并重启视频服务前要先确认")
    return await _cfg_call("local_codec", {"codec": body.codec, "sn": sn}, robotcfg.set_local_codec, body.codec,
                           expected_sn=sn)


@app.post("/api/robotcfg/remote")
async def robotcfg_remote(body: RemoteIn):
    sn = _need_real_robot()
    if not body.confirmed:
        raise HTTPException(400, "改远程配置并重启视频服务前要先确认")
    return await _cfg_call("remote", {"provider": body.provider, "codec": body.codec, "sn": sn}, robotcfg.set_remote,
                           body.provider, body.codec, expected_sn=sn)


@app.post("/api/robotcfg/sudo")
async def robotcfg_sudo(body: SudoIn):
    _need_real_robot()
    await _cfg_call("sudo", {}, robotcfg.set_sudo_password, body.password)
    return {"sudo_ready": True}


@app.delete("/api/robotcfg/sudo")
def robotcfg_sudo_forget():
    robotcfg.forget_sudo_password()
    return {"sudo_ready": False}


@app.post("/api/robotcfg/restore")
async def robotcfg_restore(body: ConfirmIn):
    sn = _need_real_robot()
    if not body.confirmed:
        raise HTTPException(400, "还原配置并重启视频服务前要先确认")
    return await _cfg_call("restore", {"sn": sn}, robotcfg.restore, expected_sn=sn)


# ---------- 设置与说明 ----------
@app.get("/api/config")
def get_config():
    return config.load()


@app.post("/api/config")
def set_config(body: dict):
    return config.save(body)


@app.get("/api/docs", response_class=PlainTextResponse)
def docs():
    return DOCS.read_text(encoding="utf-8") if DOCS.exists() else "# 使用说明\n\n还没有写。"


if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        target = DIST / path
        if path and target.is_file():
            return FileResponse(target)
        return FileResponse(DIST / "index.html")
