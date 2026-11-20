"""端到端视频延迟（双相机闪烁块法）：列相机、截图预览、打开闪烁页、录像并分析。

录像和分析直接调用 tools/ 下的 cam_capture.sh、flash_latency.py（和 11-04、11-06 实测用的是同一份）。
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, db

TOOLS = Path(__file__).resolve().parent.parent / "tools"

_FLIP_RE = re.compile(r"^([AB])：(\d+) 帧 ([\d.]+) fps.*翻转 (\d+) 次", re.M)
_PAIR_RE = re.compile(r"配上 (\d+) 次翻转")
_STAT_RE = re.compile(
    r"均值 (-?[\d.]+)（±([\d.]+)，95% 置信），中位数 (-?[\d.]+)，标准差 ([\d.]+)，P5 (-?[\d.]+)，P95 (-?[\d.]+)"
    r"，最小 (-?[\d.]+)，最大 (-?[\d.]+)")


def parse_output(text: str) -> dict:
    """flash_latency.py 的输出 → 数字。拿不到统计行时 ok=False，并带上原文便于排查。"""
    out: dict = {"ok": False, "raw": text.strip()[-2000:]}
    for cam, frames, fps, flips in _FLIP_RE.findall(text):
        out[cam] = {"frames": int(frames), "fps": float(fps), "flips": int(flips)}
    m = _PAIR_RE.search(text)
    if m:
        out["pairs"] = int(m.group(1))
    m = _STAT_RE.search(text)
    if m:
        keys = ("mean", "ci95", "median", "std", "p5", "p95", "min", "max")
        out.update({k: float(v) for k, v in zip(keys, m.groups())})
        out["ok"] = True
    flips = min(out.get("A", {}).get("flips", 0), out.get("B", {}).get("flips", 0))
    if out["ok"] and flips and out.get("pairs", 0) < flips / 2:
        out["warning"] = "配上的翻转不到一半，检查两台相机是否都拍到了闪烁块、画面是否过曝"
    return out


def list_cameras() -> list[dict]:
    """每台 USB 相机取第一个视频节点（index 0），带上 USB 口，便于判断两台是否插在同一个 HUB。"""
    base = Path("/sys/class/video4linux")
    cams = []
    for node in sorted(base.glob("video*"), key=lambda p: int(p.name[5:])):
        try:
            if (node / "index").read_text().strip() != "0":
                continue
            name = (node / "name").read_text().strip()
            usb = os.path.realpath(node / "device").split("/")[-2]
        except OSError:
            continue
        cams.append({"dev": f"/dev/{node.name}", "name": name, "usb": usb})
    return cams


def snapshot(dev: str, size: str) -> bytes:
    cfg = config.load()
    cmd = [cfg["ffmpeg"], "-hide_banner", "-loglevel", "error", "-f", "v4l2", "-input_format", "mjpeg",
           "-video_size", size, "-i", dev, "-vf", "select=gte(n\\,15),scale=960:-1", "-frames:v", "1",
           "-f", "image2", "-vcodec", "mjpeg", "pipe:1"]
    proc = subprocess.run(cmd, capture_output=True, timeout=15)
    if proc.returncode != 0 or not proc.stdout:
        err = proc.stderr.decode(errors="replace").strip().splitlines()
        msg = err[-1] if err else "没有输出"
        if "No space left on device" in msg:
            msg = "USB 带宽不够：两台相机不要插在同一个 USB 2.0 HUB 上"
        raise RuntimeError(f"{dev} 截图失败：{msg}")
    return proc.stdout


def open_flash_page(monitor: int) -> None:
    subprocess.Popen(["bash", str(TOOLS / "flash_fullscreen.sh"), str(monitor)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


@dataclass
class LatencyJob:
    kind: str = ""  # calib / measure
    name: str = ""
    stem: str = ""  # 文件名前缀 = 名称_时分秒，同名多次测量也不会互相覆盖
    phase: str = "idle"  # recording / analyzing / done / failed
    seconds: int = 30
    started_at: float = 0
    result: dict = field(default_factory=dict)
    error: str = ""
    record_id: int | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class LatencyController:
    def __init__(self):
        self.job = LatencyJob()

    def latest_calib(self) -> float | None:
        for run in db.list_latency_runs():
            if run["kind"] == "calib" and run["result"].get("ok"):
                return run["result"]["mean"]
        return None

    def start(self, *, kind: str, name: str, seconds: int, cam_a: str, cam_b: str, link: str, robot: str,
              version: str, note: str) -> LatencyJob:
        if self.job.phase in ("recording", "analyzing"):
            raise ValueError("正在录像或分析，等这一次结束")
        if kind not in ("calib", "measure"):
            raise ValueError("kind 只能是 calib 或 measure")
        if not cam_a or not cam_b or cam_a == cam_b:
            raise ValueError("请选两台不同的相机：A 拍屏幕，B 拍 Pico 镜片")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
            raise ValueError("名称只能用字母、数字、下划线和短横线")
        lost = [f"{label}（{dev}）" for label, dev in (("相机 A", cam_a), ("相机 B", cam_b))
                if dev.startswith("/dev/video") and not os.path.exists(dev)]
        if lost:
            raise ValueError(f"{'、'.join(lost)} 不在了：USB 掉线或被拔，重新插好后刷新相机列表")
        calib = None
        if kind == "measure":
            calib = self.latest_calib()
            if calib is None:
                raise ValueError("还没有校准结果，先做一次校准")
        self.job = LatencyJob(kind=kind, name=name, stem=f"{name}_{time.strftime('%H%M%S')}", phase="recording",
                              seconds=seconds, started_at=time.time())
        asyncio.get_running_loop().create_task(
            self._run(cam_a, cam_b, calib, dict(link=link, robot=robot, version=version, note=note)))
        return self.job

    async def _run(self, cam_a: str, cam_b: str, calib: float | None, meta: dict) -> None:
        job = self.job
        cfg = config.load()
        out_dir = db.data_dir() / "latency" / time.strftime("%Y-%m-%d")
        out_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, FFMPEG=cfg["ffmpeg"])
        try:
            rec = await asyncio.create_subprocess_exec(
                "bash", str(TOOLS / "cam_capture.sh"), job.stem, str(job.seconds), cfg["video_size"], cam_a, cam_b,
                cwd=out_dir, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            rec_out = (await rec.communicate())[0].decode(errors="replace")
            job.result = {"raw": rec_out.strip()[-2000:]}
            a, b = out_dir / f"{job.stem}_A.mkv", out_dir / f"{job.stem}_B.mkv"
            # 两路都要是这一次新录的、并且录全（> 1 MB）；脚本收尾统计帧数出错不算录像失败
            bad = [(label, dev) for label, dev, p in (("相机 A", cam_a, a), ("相机 B", cam_b, b))
                   if not p.exists() or p.stat().st_size < 1_000_000 or p.stat().st_mtime < job.started_at]
            if bad:
                who = "、".join(f"{label}（{dev}）" for label, dev in bad)
                if "No space left" in rec_out:
                    hint = f"{who} 打不开：USB 带宽不够，两台相机不要插在同一个 USB 2.0 HUB 上"
                elif any(not os.path.exists(dev) for _, dev in bad if dev.startswith("/dev/video")):
                    hint = f"{who} 录像途中掉线了：检查 USB 线和接口，重新插好后刷新相机列表再测"
                else:
                    hint = f"{who} 没录到，看下面的原始输出"
                raise RuntimeError(f"录像失败：{hint}")
            job.phase = "analyzing"
            args = [cfg["python"], str(TOOLS / "flash_latency.py"), str(a), str(b), "--csv", str(out_dir / f"{job.stem}.csv")]
            if calib is not None:
                args += ["--calib", f"{calib:.2f}"]
            ana = await asyncio.create_subprocess_exec(*args, cwd=out_dir, env=env, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.STDOUT)
            text = (await ana.communicate())[0].decode(errors="replace")
            result = parse_output(text)
            result["files"] = [str(a), str(b)]
            result["csv"] = str(out_dir / f"{job.stem}.csv")
            job.result = result
            if not result["ok"]:
                raise RuntimeError("分析没有得出统计结果：两路画面里没找到能配对的闪烁翻转，看下面的原始输出，"
                                   "检查两台相机是否都拍到了闪烁块")
            row = db.add_latency_run(job.kind, job.name, result, calib_ms=calib, **meta)
            job.record_id = row["id"]
            job.phase = "done"
        except Exception as exc:
            job.phase = "failed"
            job.error = str(exc)
