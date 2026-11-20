"""机器人连接：8080 读身份，5000 端口 WebSocket 收发 SDK 指令（协议同 Oli Robot Manager）。

演练模式用 SimRobot，不连真机，用来熟悉流程和自测界面；界面上始终标明“演练”。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
import uuid
from dataclasses import dataclass, field

import httpx
import websockets

from .db import data_dir

PORTAL = "http://10.192.1.2:8080"
WS_URL = "ws://10.192.1.2:5000"
_SN_RE = re.compile(r"(HU_[A-Z0-9]+(?:_[A-Z0-9]+){1,3}|TRON2A_\d+|WF_[A-Z0-9]+(?:_[A-Z0-9]+)+)", re.I)


def extract_sn(text: str) -> str:
    m = _SN_RE.search(text or "")
    return m.group(1).upper() if m else ""


@dataclass
class Identity:
    sn: str = ""
    version: str = ""
    robot_status: str = ""
    battery: str = ""
    reachable: bool = False
    simulated: bool = False
    checked_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


async def read_identity(timeout: float = 2.5) -> Identity:
    """先读 8080 拿 SN，再听一帧 notify_robot_info 拿状态和固件版本。"""
    ident = Identity()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for path in ("/get_robot_info", "/get_local_version", "/"):
                r = await client.get(PORTAL + path)
                if r.status_code == 200:
                    ident.sn = extract_sn(r.text)
                    if ident.sn:
                        break
        ident.reachable = True
    except httpx.HTTPError:
        return ident
    try:
        async with websockets.connect(WS_URL, open_timeout=timeout) as ws:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic()))
                if msg.get("title") == "notify_robot_info":
                    info = parse_robot_info(msg.get("data", {}))
                    ident.robot_status = info.get("robot_status", "")
                    ident.version = info.get("version", "")
                    ident.battery = info.get("battery", "")
                    ident.sn = ident.sn or extract_sn(info.get("sn", ""))
                    break
    except (OSError, asyncio.TimeoutError, websockets.WebSocketException):
        pass
    return ident


def parse_robot_info(data: dict) -> dict:
    info: dict = {}
    for item in data.get("result", []):
        for v in item.get("values", []):
            key, value = v.get("key", ""), v.get("value", "")
            if item.get("name") == "system_info" and key in ("robot_status", "ability_running", "mode", "sn", "version"):
                info[key] = str(value)
            elif item.get("name") == "peripheral" and key == "battery":
                info["battery"] = f"{value}%"
    return info


def _audit(event: str, accid: str, title: str, guid: str, payload: dict, result: dict | None = None) -> None:
    rec = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, "accid": accid,
           "title": title, "guid": guid, "data": payload, "result": result or {}}
    with open(data_dir() / "sdk_audit.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class SdkError(RuntimeError):
    pass


class RealRobot:
    simulated = False

    def __init__(self, accid: str):
        if not accid:
            raise SdkError("没有识别到机器人 SN，已阻止发送指令")
        self.accid = accid

    async def request(self, title: str, data: dict | None = None, timeout: float = 30.0) -> dict:
        guid = uuid.uuid4().hex
        payload = data or {}
        _audit("send", self.accid, title, guid, payload)
        try:
            async with websockets.connect(WS_URL, open_timeout=5) as ws:
                await ws.send(json.dumps({"accid": self.accid, "title": title,
                                          "timestamp": int(time.time() * 1000), "guid": guid, "data": payload}))
                want = title.replace("request_", "response_", 1)
                deadline = time.monotonic() + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SdkError(f"{title} {timeout:.0f} s 内没有返回")
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
                    if msg.get("title") == want and msg.get("guid") == guid:
                        result = msg.get("data", {})
                        _audit("response", self.accid, want, guid, payload, result)
                        return result
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
            _audit("error", self.accid, title, guid, payload, {"error": str(exc)})
            raise SdkError(f"{title} 发送失败：{exc}") from exc


class SimRobot:
    """演练用：按真机的大致耗时返回，偶尔失败。"""
    simulated = True
    accid = "SIM_OLI"

    def __init__(self, fail_rate: float = 0.04, seed: int | None = None):
        self.fail_rate = fail_rate
        self.rng = random.Random(seed)

    async def request(self, title: str, data: dict | None = None, timeout: float = 30.0) -> dict:
        base = {"request_from_stand_to_sit": 3.2, "request_lie_down": 4.5, "request_standup": 5.0,
                "request_set_walk_mode": 0.4, "request_set_motion_engine": 0.6}.get(title, 0.3)
        await asyncio.sleep(base * self.rng.uniform(0.15, 0.25))  # 演练加速到 1/5
        if self.rng.random() < self.fail_rate:
            return {"result": "fail_timeout"}
        return {"result": "success"}
