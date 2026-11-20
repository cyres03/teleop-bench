"""本地配置（~/.teleop-bench/config.json）：SSH 登录方式、工具路径。不存密码。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import paramiko

from .db import data_dir

def _default_ffmpeg() -> str:
    """优先用 imageio-ffmpeg 自带的静态 ffmpeg（带 v4l2 和 libx264），装在本软件的 Python 环境里。"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


DEFAULTS = {
    "ssh_host": "10.192.1.2",
    "ssh_user": "limx",
    "ssh_key": "~/.ssh/oli_robot_manager_ed25519",
    "video_ssh_host": "10.192.1.3",  # 视频服务 pico_teleop_server 在 1.3
    "video_ssh_user": "guest",
    "camera_a": "",
    "camera_b": "",
    "video_size": "2560x720",
    "ffmpeg": os.environ.get("FFMPEG", "") or _default_ffmpeg(),
    "lark_cli": "",
    "python": os.environ.get("BENCH_ANALYSIS_PYTHON", sys.executable),
}


def load() -> dict:
    path = data_dir() / "config.json"
    cfg = dict(DEFAULTS)
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    return cfg


def save(updates: dict) -> dict:
    cfg = load()
    for k, v in updates.items():
        if k in DEFAULTS:
            cfg[k] = str(v)
    (data_dir() / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


class _KnownAnywhere(paramiko.MissingHostKeyPolicy):
    """10.192.1.2 在每台机器人上都一样，主机密钥分散记在 ~/.ssh/known_hosts*。
    只要服务器密钥出现在任一份记录里就接受，否则拒绝；连上后再用 SN 核对是哪台。"""

    def __init__(self, files: list[Path]):
        self.keys = set()
        for f in files:
            try:
                hk = paramiko.HostKeys(str(f))
            except (OSError, paramiko.SSHException):
                continue
            for entry in hk.values():
                for key in entry.values():
                    self.keys.add(key.asbytes())

    def missing_host_key(self, client, hostname, key):
        if key.asbytes() not in self.keys:
            raise paramiko.SSHException(f"{hostname} 的主机密钥不在本机记录里，已拒绝连接")


def ssh_client(host: str | None = None, user: str | None = None) -> paramiko.SSHClient:
    """用密钥登录主控（默认 1.2，视频配置用 1.3）；主机密钥必须已在 ~/.ssh/known_hosts* 里，未知主机直接拒绝。"""
    cfg = load()
    files = sorted(Path("~/.ssh").expanduser().glob("known_hosts*"))
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(_KnownAnywhere(files))
    client.connect(host or cfg["ssh_host"], username=user or cfg["ssh_user"], key_filename=str(Path(cfg["ssh_key"]).expanduser()),
                   timeout=8, allow_agent=False, look_for_keys=False)
    return client
