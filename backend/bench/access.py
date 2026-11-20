"""第一次连某台机器人：在软件里输一次机器人密码，授权本机密钥登录（和 robot-access.sh 做的事一样）。

1. 本机没有密钥就生成 ~/.ssh/oli_robot_manager_ed25519（私钥只在本机）；
2. 用密码登录一次，把这台机器人的主机指纹追加到 ~/.ssh/known_hosts_teleop_bench；
3. 把本机公钥加进机器人的 ~/.ssh/authorized_keys，再用密钥登录核对。
密码只用这一次，不写盘、不回显。
"""
from __future__ import annotations

import shlex
import socket
import subprocess
from pathlib import Path

import paramiko

from . import config

KH = Path("~/.ssh/known_hosts_teleop_bench").expanduser()


def _key() -> Path:
    return Path(config.load()["ssh_key"]).expanduser()


def ensure_key() -> Path:
    key = _key()
    if not key.exists():
        key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"teleop-bench@{socket.gethostname()}",
                        "-f", str(key)], check=True, capture_output=True, timeout=30)
    return key


def key_ok(host: str, user: str) -> bool:
    if not _key().exists():
        return False
    try:
        config.ssh_client(host, user).close()
        return True
    except Exception:  # noqa: BLE001
        return False


def status() -> dict:
    cfg = config.load()
    return {"main": key_ok(cfg["ssh_host"], cfg["ssh_user"]), "video": key_ok(cfg["video_ssh_host"], cfg["video_ssh_user"])}


def authorize(host: str, user: str, password: str) -> None:
    if not password:
        raise ValueError(f"填 {user} 的密码")
    pub = ensure_key().with_suffix(".pub").read_text().strip()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(host, username=user, password=password, timeout=8, allow_agent=False, look_for_keys=False)
    except paramiko.AuthenticationException:
        raise ValueError(f"{user} 的密码不对") from None
    except (OSError, paramiko.SSHException) as exc:
        raise ValueError(f"连不上 {host}：确认电脑连着机器人热点（{exc.__class__.__name__}）") from None
    try:
        hk = c.get_transport().get_remote_server_key()
        line = f"{host} {hk.get_name()} {hk.get_base64()}"
        KH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        old = KH.read_text().splitlines() if KH.exists() else []
        if line not in old:
            KH.write_text("\n".join(old + [line]) + "\n")
        q = shlex.quote(pub)
        _, o, e = c.exec_command("mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && "
                                 f"chmod 600 ~/.ssh/authorized_keys && (grep -qxF {q} ~/.ssh/authorized_keys || echo {q} >> ~/.ssh/authorized_keys)",
                                 timeout=20)
        if o.channel.recv_exit_status():
            raise ValueError(f"机器人上写授权失败：{e.read().decode(errors='replace')[-200:]}")
    finally:
        c.close()
    if not key_ok(host, user):
        raise ValueError(f"授权后密钥还是登不上 {user}@{host}")
