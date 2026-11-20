"""机器人视频配置：读、改近场编码和远程链路（阿里云 LiveKit / 腾讯云 TRRO），改前自动备份，可一键还原。

视频服务 pico_teleop_server 跑在 1.3（root，mroslaunch 管理），读的是 1.3 自己的
/opt/limx/install/etc/livekit/configs/<CONFIG_FILE>。1.2 上也有同名文件，但服务不读它，所以一律在 1.3 上读和改。

- 登录：guest@10.192.1.3，用本机密钥；主机密钥必须在 ~/.ssh/known_hosts* 里。
- 1.3 的 /opt/limx 是 1.2 的 CIFS 共享，1.3 上 root 也写不了：配置写在 1.2（limx，sudo 免密），在 1.3 核对 md5、重启服务。
  （sudo 密码相关函数保留，当前流程用不到。）
- 腾讯云 TRRO 的编码实际看 trro/config.json 每路视频的 codec（0=H264，1=H265），yaml 的 trro.codec 对它无效；切腾讯云编码时两个文件一起改。
- 改完用 mrosnode 重启 pico_teleop_server，再看 1.3 上的进程：pid 变了才算重启成功；任何一步不对都报错，不吞错误。

改配置、重启视频服务都是改机器人状态：界面上每次都要人点确认才会调用这里。
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import time

from . import config

CFG_DIR = "/opt/limx/install/etc/livekit/configs"
AUTOLAUNCH = "/opt/limx/install/etc/autolaunch/robot-app.yaml"
LOCAL_CODECS = ("jpeg", "h264", "h265")
REMOTE_CODECS = ("h264", "h265")
PROVIDERS = {"aliyun": ("livekit", "阿里云"), "tencent": ("trro", "腾讯云")}
# 腾讯云 TRRO 的编码实际只看这个文件里每路视频的 codec（0=H264，1=H265）；yaml 里 trro.codec 对它无效
TRRO_JSON = f"{CFG_DIR}/trro/config.json"
TRRO_CODEC = {"h264": 0, "h265": 1}
SECTION_NAME = {v[0]: v[1] for v in PROVIDERS.values()}
MROS = "source /opt/limx/install/setup.bash >/dev/null 2>&1; export MROS_IP_LIST=10.192.1.x; "
NODE = "pico_teleop_server"
RESTART_WAIT_S = 25.0
_SUDO: dict[str, str | None] = {"pw": None}


# ---------------- 配置文本：解析和修改（纯函数，可离线测） ----------------
_TOP = re.compile(r"^([A-Za-z_][\w-]*):")
_SUB = re.compile(r"^  ([A-Za-z_][\w-]*):\s*(#.*)?$")
_KV = re.compile(r"^(\s+)(codec|enabled|url):(\s*)([^\s#]+)(.*)$")


def _walk(text: str):
    """逐行给出 (行号, 一级段, 远程子段, 缩进, 键, 值)；不是 codec/enabled/url 的行键为 None。"""
    top = sub = None
    for i, line in enumerate(text.splitlines()):
        m = _TOP.match(line)
        if m:
            top, sub = m.group(1), None
            yield i, top, sub, "", None, None
            continue
        if top == "remote":
            ms = _SUB.match(line)
            if ms:
                sub = ms.group(1)
        mk = _KV.match(line)
        if mk:
            yield i, top, sub, mk.group(1), mk.group(2), mk.group(4).strip('"')
        else:
            yield i, top, sub, "", None, None


def parse_cfg(text: str) -> dict:
    s: dict = {"local_codec": None, "trro": {}, "livekit": {}}
    for _, top, sub, ind, key, val in _walk(text):
        if key is None:
            continue
        if top == "local" and ind == "  " and key == "codec":
            s["local_codec"] = val.lower()
        elif top == "remote" and sub in ("trro", "livekit") and ind == "    ":
            s[sub][key] = (val.lower() == "true") if key == "enabled" else val
    t, lk = bool(s["trro"].get("enabled")), bool(s["livekit"].get("enabled"))
    if t and lk:
        prov, err = None, "trro 和 livekit 同时 enabled=true（配置冲突）"
    elif t:
        prov, err = "tencent", ""
    elif lk:
        prov, err = "aliyun", ""
    else:
        prov, err = None, "远程没有启用（trro、livekit 都是 false）"
    sec = PROVIDERS[prov][0] if prov else None
    s.update(remote=prov, remote_name=PROVIDERS[prov][1] if prov else "无",
             remote_codec=(s[sec].get("codec") or "").lower() if sec else None, remote_error=err,
             livekit_url=s["livekit"].get("url", ""))
    return s


def edit_cfg(text: str, *, local_codec: str | None = None, remote: str | None = None,
             remote_codec: str | None = None) -> str:
    """只改需要的那几行的值，保留缩进、注释和其他内容；改完解析回来核对。"""
    if local_codec is not None and local_codec not in LOCAL_CODECS:
        raise ValueError("近场编码只能是 jpeg / h264 / h265")
    if remote is not None and remote not in PROVIDERS:
        raise ValueError("远程只能是 aliyun（阿里云 LiveKit）或 tencent（腾讯云 TRRO）")
    if remote_codec is not None:
        if remote_codec not in REMOTE_CODECS:
            raise ValueError("远程编码只能是 h264 / h265")
        if remote is None:
            raise ValueError("改远程编码要同时指定远程链路")
    lines = text.splitlines()
    on = PROVIDERS[remote][0] if remote else None
    hit = {"local": False, "trro_en": False, "livekit_en": False, "codec": False}
    for i, top, sub, ind, key, _ in _walk(text):
        if key is None:
            continue
        new = None
        if local_codec and top == "local" and ind == "  " and key == "codec":
            new, hit["local"] = local_codec, True
        elif remote and top == "remote" and sub in ("trro", "livekit") and ind == "    ":
            if key == "enabled":
                new = "true" if sub == on else "false"
                hit[f"{sub}_en"] = True
            elif key == "codec" and sub == on and remote_codec:
                new, hit["codec"] = remote_codec, True
        if new is not None:
            m = _KV.match(lines[i])
            lines[i] = f"{m.group(1)}{m.group(2)}:{m.group(3) or ' '}{new}{m.group(5)}"
    need = (["local"] if local_codec else []) + (["trro_en", "livekit_en"] if remote else []) + \
           (["codec"] if remote_codec else [])
    missing = [k for k in need if not hit[k]]
    if missing:
        raise ValueError(f"配置文件里找不到要改的行：{missing}，文件格式变了，已停止")
    out = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    got = parse_cfg(out)
    if (local_codec and got["local_codec"] != local_codec) or (remote and got["remote"] != remote) or \
            (remote_codec and got["remote_codec"] != remote_codec):
        raise RuntimeError("改完解析回来和目标不一致，已停止")
    return out


def trro_codec(text: str) -> str:
    """trro/config.json 里各路视频的编码：h264 / h265；各路不一致返回 mixed。"""
    streams = json.loads(text).get("streams_config") or []
    names = {k: v for v, k in TRRO_CODEC.items()}
    got = {names.get(st.get("codec"), f"codec={st.get('codec')}") for st in streams}
    if not got:
        raise ValueError("trro/config.json 里没有 streams_config")
    return got.pop() if len(got) == 1 else "mixed"


def edit_trro_json(text: str, codec: str) -> str:
    """把每路视频的 codec 改成目标值，只替换数字，保留原有格式；改完解析回来核对。"""
    if codec not in TRRO_CODEC:
        raise ValueError("腾讯云编码只能是 h264 / h265")
    n = len(json.loads(text).get("streams_config") or [])
    out, k = re.subn(r'("codec"\s*:\s*)-?\d+', lambda m: f"{m.group(1)}{TRRO_CODEC[codec]}", text)
    if k != n or trro_codec(out) != codec:
        raise RuntimeError(f"trro/config.json 格式和预期不同（{n} 路视频，找到 {k} 个 codec），已停止")
    return out


def label(state: dict, link: str) -> str:
    """按机器人实际配置，给出这条链路正在用的编码，例如 h264；链路没启用返回空。"""
    if link == "近场":
        return state.get("local_codec") or ""
    name = link.replace("远程", "").strip()
    return (state.get("remote_codec") or "") if state.get("remote_name") == name else ""


def check_match(state: dict, link: str, codec: str) -> str:
    """要测的链路、编码和机器人实际配置一致返回空串，否则返回原因。"""
    codec = codec.lower()
    if link == "近场":
        actual = state.get("local_codec")
        return "" if actual == codec else f"机器人近场编码是 {str(actual).upper()}，要测的是 {codec.upper()}"
    if state.get("remote_error"):
        return f"机器人远程配置有问题：{state['remote_error']}"
    want = link.replace("远程", "").strip()
    if state.get("remote_name") != want:
        return f"机器人远程用的是 {state.get('remote_name')}，要测的是 {want}"
    if state.get("remote_codec") != codec:
        return f"机器人{want}编码是 {str(state.get('remote_codec')).upper()}，要测的是 {codec.upper()}"
    return ""


# ---------------- SSH ----------------
def _writer():
    """1.3 的 /opt/limx 是 1.2 的 CIFS 共享（//10.192.1.2/OptSharedFolder），1.3 上 root 也写不了；
    配置文件实际在 1.2，用 1.2 的 limx 写（sudo 免密），服务在 1.3 重启。"""
    return config.ssh_client()


def _client():
    cfg = config.load()
    return config.ssh_client(host=cfg["video_ssh_host"], user=cfg["video_ssh_user"])


def _run(client, cmd: str, timeout: int = 40, stdin: str | None = None) -> str:
    chan_in, out, err = client.exec_command(cmd, timeout=timeout)
    if stdin is not None:
        chan_in.write(stdin)
        chan_in.flush()
        chan_in.channel.shutdown_write()
    text = out.read().decode(errors="replace")
    code = out.channel.recv_exit_status()
    if code != 0:
        msg = err.read().decode(errors="replace").strip()
        if _SUDO["pw"]:
            msg = msg.replace(_SUDO["pw"], "***")
        raise RuntimeError(f"命令失败（{code}）：{msg[-300:]}")
    return text


def _cfg_path(client) -> str:
    name = _run(client, f"sed -n 's/^ *CONFIG_FILE: *//p' {AUTOLAUNCH} | head -1").strip() or "oli_edu_mros.yaml"
    return f"{CFG_DIR}/{name}"


def _sn(client) -> str:
    return _run(client, "sed -n 's/^sn: *//p' /opt/limx/imptdata/sn.yaml").strip()


def _node(client) -> dict:
    """视频服务进程：直接看 1.3 上的进程（mrosnode status 要等 10 s 发现）。重启前后 pid 会变。"""
    text = _run(client, "p=$(pgrep -f '^/opt/limx/install/bin/pico_teleop_server' | head -1); "
                        "[ -n \"$p\" ] && ps -o pid=,lstart= -p $p || true", timeout=15).strip()
    m = re.match(r"(\d+)\s+\w+\s+(\w+)\s+(\d+)\s+([\d:]+)\s+(\d+)", text)
    if not m:
        return {"state": "stopped", "start_time": "", "pid": "", "raw": text[-200:]}
    pid, mon, day, hms, year = m.groups()
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    st = f"{year}-{months.index(mon) + 1:02d}-{int(day):02d} {hms}" if mon in months else f"{mon} {day} {hms}"
    return {"state": "running", "start_time": st, "pid": pid, "raw": ""}


def _trro_state(client, s: dict) -> dict:
    """腾讯云编码以 trro/config.json 为准；读不到时保留 yaml 值并说明。"""
    try:
        actual = trro_codec(_run(client, f"cat {TRRO_JSON}"))
    except (RuntimeError, ValueError) as exc:
        return {"trro_json_codec": None, "trro_json_error": str(exc)[-200:]}
    out = {"trro_json_codec": actual, "trro_json_error": ""}
    if s.get("remote") == "tencent":
        out["remote_codec"] = actual
    return out


def read_state() -> dict:
    client = _client()
    try:
        path = _cfg_path(client)
        text = _run(client, f"cat {shlex.quote(path)}")
        s = parse_cfg(text)
        s.update(_trro_state(client, s))
        w = _writer()
        try:
            backups = _run(w, "ls -d ~/Cyres/backup_video_* 2>/dev/null | tail -3 || true").split()
        finally:
            w.close()
        return {"path": path, "host": config.load()["video_ssh_host"], **s, "backups": backups,
                "sn": _sn(client), "node": _node(client), "sudo_ready": True}
    finally:
        client.close()


# ---------------- sudo 密码（只在内存） ----------------
def set_sudo_password(password: str) -> None:
    if not password:
        raise ValueError("密码为空")
    client = _client()
    try:
        _run(client, "sudo -S -k -p '' true", stdin=password + "\n", timeout=20)
    except RuntimeError:
        raise ValueError("sudo 密码不对（1.3 的 guest 账号）") from None
    finally:
        client.close()
    _SUDO["pw"] = password


def forget_sudo_password() -> None:
    _SUDO["pw"] = None


def _sudo(client, cmd: str) -> str:
    if not _SUDO["pw"]:
        raise ValueError("先在界面输入 1.3 的 sudo 密码（只存在本次运行的内存里）")
    try:
        return _run(client, f"sudo -S -k -p '' {cmd}", stdin=_SUDO["pw"] + "\n")
    except RuntimeError as exc:
        if "incorrect password" in str(exc) or "Sorry" in str(exc):
            _SUDO["pw"] = None
            raise ValueError("sudo 密码不对，已清除，重新输入") from None
        raise


# ---------------- 写配置、重启服务 ----------------
def _install(client, path: str, new_text: str, *, backup_first: bool) -> str:
    """client 是 1.3（用来核对）；写入走 1.2。"""
    w = _writer()
    try:
        if backup_first:
            backup = f"~/Cyres/backup_video_{time.strftime('%Y%m%d')}"
            _run(w, f"mkdir -p {backup}; [ -f {backup}/$(basename {path}) ] || cp -p {path} {backup}/")
        else:
            backup = ""
        tmp = "/tmp/.tb_video_cfg.yaml"
        sftp = w.open_sftp()
        try:
            with sftp.open(tmp, "w") as f:
                f.write(new_text.encode())
        finally:
            sftp.close()
        try:
            _run(w, f"sudo -n install -m 644 -o root -g root {tmp} {path}")
        finally:
            _run(w, f"rm -f {tmp}")
    finally:
        w.close()
    want = hashlib.md5(new_text.encode()).hexdigest()
    for _ in range(10):  # CIFS 缓存 1 s
        if _run(client, f"md5sum {path}").split()[0] == want:
            return backup
        time.sleep(0.5)
    raise RuntimeError("1.3 上读到的配置和写进去的不一致")


def _restart(client, wait_s: float | None = None) -> dict:
    before = _node(client)
    _run(client, MROS + f"mrosnode restart {NODE}", timeout=60)
    deadline = time.time() + (wait_s or RESTART_WAIT_S)
    now = before
    while time.time() < deadline:
        time.sleep(1.5)
        now = _node(client)
        if now["state"] == "running" and now["pid"] and now["pid"] != before["pid"]:
            return now
    raise RuntimeError(f"{NODE} 没有重启成功：重启前 pid {before['pid'] or '?'}（{before['start_time'] or '?'}），"
                       f"现在 {now['state']} pid {now['pid'] or '-'} {now['start_time'] or now['raw']}")


def apply(*, expected_sn: str, local_codec: str | None = None, remote: str | None = None,
          remote_codec: str | None = None) -> dict:
    """改近场编码或远程链路（含远程编码），备份 → 写入 → 重启 → 读回核对。"""
    client = _client()
    try:
        sn = _sn(client)
        if expected_sn and sn != expected_sn:
            raise ValueError(f"SSH 连到的是 {sn}，不是界面上识别的 {expected_sn}，已停止")
        path = _cfg_path(client)
        old = _run(client, f"cat {shlex.quote(path)}")
        new = edit_cfg(old, local_codec=local_codec, remote=remote, remote_codec=remote_codec)
        old_json = new_json = None
        if remote == "tencent" and remote_codec:
            old_json = _run(client, f"cat {TRRO_JSON}")
            new_json = edit_trro_json(old_json, remote_codec)
        if new == old and new_json == old_json:
            return {**read_state(), "message": "机器人上已经是这个配置，没有改动，也没有重启"}
        backup = ""
        if new != old:
            backup = _install(client, path, new, backup_first=True)
        if new_json != old_json:
            backup = _install(client, TRRO_JSON, new_json, backup_first=True) or backup
        node = _restart(client)
        got = parse_cfg(_run(client, f"cat {shlex.quote(path)}"))
        got.update(_trro_state(client, got))
        what = []
        if local_codec:
            what.append(f"近场 {got['local_codec'].upper()}")
        if remote:
            what.append(f"远程 {got['remote_name']} {str(got['remote_codec']).upper()}")
        return {**read_state(), "backup": backup,
                "message": f"已改成 {'、'.join(what)}，视频服务 {node['start_time']} 重启；Pico 需要重新连接"}
    finally:
        client.close()


def set_local_codec(codec: str, *, expected_sn: str) -> dict:
    return apply(expected_sn=expected_sn, local_codec=codec.lower())


def set_remote(provider: str, codec: str, *, expected_sn: str) -> dict:
    return apply(expected_sn=expected_sn, remote=provider, remote_codec=codec.lower())


def restore(*, expected_sn: str) -> dict:
    client = _client()
    try:
        sn = _sn(client)
        if expected_sn and sn != expected_sn:
            raise ValueError(f"SSH 连到的是 {sn}，不是界面上识别的 {expected_sn}，已停止")
        path = _cfg_path(client)
        w = _writer()
        try:
            first = _run(w, "ls -d ~/Cyres/backup_video_* 2>/dev/null | head -1 || true").strip()
            if not first:
                raise ValueError("没有备份，配置没被本软件改过")
            text = _run(w, f"cat {first}/$(basename {path})")
            json_text = _run(w, f"cat {first}/config.json 2>/dev/null || true")
        finally:
            w.close()
        _install(client, path, text, backup_first=False)
        if json_text.strip():
            _install(client, TRRO_JSON, json_text, backup_first=False)
        node = _restart(client)
        return {**read_state(), "message": f"已用 {first} 还原，视频服务 {node['start_time']} 重启"}
    finally:
        client.close()
