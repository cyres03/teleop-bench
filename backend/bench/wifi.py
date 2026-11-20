"""连机器人热点（nmcli）。

优先用 USB 无线网卡（wlx 开头），这样电脑自带网卡还能留着上外网；没有 USB 网卡时用自带网卡，并提示会断外网。
已保存过的热点直接 `nmcli con up`；没保存过的要输入密码，用 `nmcli dev wifi connect`。
"""
from __future__ import annotations

import subprocess

PREFIX = ("HU_", "DACH_", "WF_", "PF_", "SF_")


def _run(args: list[str], timeout: float = 30) -> str:
    p = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout).strip()[-300:] or f"nmcli 失败（{p.returncode}）")
    return p.stdout


def _split(line: str) -> list[str]:
    out, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def devices() -> list[dict]:
    rows = []
    for line in _run(["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev"]).splitlines():
        d, t, st, con = (_split(line) + ["", "", "", ""])[:4]
        if t == "wifi":
            rows.append({"device": d, "state": st, "connection": con, "usb": d.startswith("wlx")})
    return rows


def pick_device() -> dict | None:
    devs = devices()
    return next((d for d in devs if d["usb"]), None) or (devs[0] if devs else None)


def saved() -> set[str]:
    return {(_split(l) + [""])[0] for l in _run(["-t", "-f", "NAME,TYPE", "con", "show"]).splitlines() if "wireless" in l}


def hotspots(rescan: bool = True) -> dict:
    dev = pick_device()
    if not dev:
        return {"device": "", "usb": False, "current": "", "list": [], "error": "这台电脑没有无线网卡"}
    if rescan:
        try:
            _run(["dev", "wifi", "rescan", "ifname", dev["device"]], timeout=15)
        except Exception:
            pass  # 刚扫过时 nmcli 会拒绝，直接用缓存
    names = saved()
    seen: dict[str, dict] = {}
    for line in _run(["-t", "-f", "IN-USE,SSID,SIGNAL", "dev", "wifi", "list", "ifname", dev["device"]]).splitlines():
        use, ssid, sig = (_split(line) + ["", "", "0"])[:3]
        if not ssid.startswith(PREFIX):
            continue
        s = int(sig or 0)
        if ssid not in seen or s > seen[ssid]["signal"]:
            seen[ssid] = {"ssid": ssid, "signal": s, "saved": ssid in names, "in_use": use.strip() == "*"}
    lst = sorted(seen.values(), key=lambda x: (not x["in_use"], -x["signal"]))
    return {"device": dev["device"], "usb": dev["usb"], "current": dev["connection"], "list": lst, "error": ""}


def connect(ssid: str, password: str = "") -> str:
    if not ssid.startswith(PREFIX):
        raise ValueError("只连机器人热点")
    dev = pick_device()
    if not dev:
        raise RuntimeError("这台电脑没有无线网卡")
    if ssid in saved() and not password:
        _run(["con", "up", "id", ssid, "ifname", dev["device"]], timeout=45)
    else:
        if not password:
            raise ValueError("这个热点第一次连，要输入密码")
        _run(["dev", "wifi", "connect", ssid, "password", password, "ifname", dev["device"]], timeout=45)
    return dev["device"]
