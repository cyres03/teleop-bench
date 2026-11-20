"""LimX 夹爪夹持力测试：读默认值 → 默认、0、100 三档依次设置 → 每档操作员夹特殊塑料杯、拍照上传 → 点“已完成”才进下一档 → 测完恢复默认值。

接口：设置 /teleop/{left,right}_limx_gripper_force（std_srvs/SetFloat32，0–100），回读 /teleop/get_*（GetFloat32）。
只对本次运行有效，重启遥操服务后恢复 80。每档点“已完成”时顺带读一帧 /limx/2F-gripper/cmd，记下 tau。
"""
from __future__ import annotations

import base64
import json
import re
import shlex
import threading
import time
from pathlib import Path

from .db import data_dir
from .robotstream import MROS, _json

SIDES = {"左": ("left",), "右": ("right",), "两侧": ("left", "right")}
LEVELS = (0.0, 100.0)          # 默认值之后要测的两档
MAX_PHOTO = 8 * 1024 * 1024


def get_cmd(side: str) -> str:
    return f"{MROS}timeout 15 mrosservice call /teleop/get_{side}_limx_gripper_force std_srvs/GetFloat32 '{{}}' 2>&1 | grep -m1 response || true"


def set_cmd(side: str, value: float) -> str:
    body = shlex.quote(json.dumps({"data": float(value)}))
    return f"{MROS}timeout 15 mrosservice call /teleop/{side}_limx_gripper_force std_srvs/SetFloat32 {body} 2>&1 | grep -m1 response || true"


def cmd_sample() -> str:
    """读一帧夹爪指令（只在遥操跟随时有，10 Hz）。"""
    return f"{MROS}timeout 8 mrostopic echo /limx/2F-gripper/cmd 2>&1 | grep -m1 '{{' || true"


def plan(default: float) -> list[float]:
    out = [round(default, 1)]
    for v in LEVELS:
        if v not in out:
            out.append(v)
    return out


class GripperController:
    def __init__(self, ssh_factory=None):
        self._ssh_factory = ssh_factory
        self.robot = self.version = ""
        self.simulated = False
        self.lock = threading.Lock()
        self.busy = ""
        self.error = ""
        self.t: dict | None = None      # 当前这一轮
        self._sim_force = {"left": 80.0, "right": 80.0}

    def bind(self, *, robot: str, version: str, simulated: bool) -> None:
        if self.t and (robot, simulated) != (self.t["robot"], self.t["simulated"]):
            return  # 测着的时候换了机器人：不动当前这轮，开始新的一轮时再按新机器人
        self.robot, self.version, self.simulated = robot, version, simulated

    def _dir(self) -> Path:
        name = ("演练_" if self.simulated else "") + time.strftime("%Y-%m-%d") + "_" + (self.robot or "未识别")
        d = data_dir() / "gripper" / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def history(self) -> list[dict]:
        f = self._dir() / "runs.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []

    def state(self) -> dict:
        return {"robot": self.robot, "version": self.version, "simulated": self.simulated, "busy": self.busy,
                "error": self.error, "test": self.t, "runs": self.history(),
                "report_path": str(self._dir() / "报告.md") if (self._dir() / "报告.md").exists() else ""}

    # ---------- 主控 ----------
    def _exec(self, c, cmd: str, timeout: float = 40) -> str:
        _, o, e = c.exec_command(cmd, timeout=timeout)
        return o.read().decode(errors="replace") + e.read().decode(errors="replace")

    def _get(self, c, side: str) -> float | None:
        if self.simulated:
            return self._sim_force[side]
        m = re.search(r"response:\s*(\{.*\})", self._exec(c, get_cmd(side)))
        d = json.loads(m.group(1)) if m else {}
        return float(d["data"]) if d.get("success") and "data" in d else None

    def _set(self, c, side: str, value: float) -> bool:
        if self.simulated:
            self._sim_force[side] = value
            return True
        m = re.search(r"response:\s*(\{.*\})", self._exec(c, set_cmd(side, value)))
        return bool(m and json.loads(m.group(1)).get("success"))

    def _tau(self, c) -> list[float] | None:
        if self.simulated:
            return [self._sim_force["left"], self._sim_force["right"]]
        d = _json(self._exec(c, cmd_sample(), timeout=20))
        return [round(float(x), 1) for x in d.get("tau", [])] if d else None

    def _client(self):
        return None if self.simulated else self._ssh_factory()

    def _run(self, label: str, fn) -> None:
        if self.busy:
            raise ValueError(f"正在{self.busy}，等一下")
        self.busy, self.error = label, ""

        def go():
            c = None
            try:
                c = self._client()
                fn(c)
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)
            finally:
                if c:
                    c.close()
                self.busy = ""
        threading.Thread(target=go, daemon=True).start()

    # ---------- 流程 ----------
    def start(self, side: str) -> None:
        if side not in SIDES:
            raise ValueError("选左、右或两侧")
        if self.t and not self.t.get("done"):
            raise ValueError("上一轮还没测完：点完“已完成”或“放弃这一轮”")
        if not self.simulated and not self.robot:
            raise ValueError("没识别到机器人：先连上机器人热点")

        def fn(c):
            sides = SIDES[side]
            default = {s: self._get(c, s) for s in sides}
            if any(v is None for v in default.values()):
                raise RuntimeError("读不到夹持力：/teleop/get_*_limx_gripper_force 没响应，遥操节点在跑吗")
            if len(set(default.values())) > 1:
                raise RuntimeError(f"左右默认值不一样（{default}），先确认现场有没有人改过")
            d0 = next(iter(default.values()))
            name = time.strftime("%H%M%S")
            self.t = {"name": name, "side": side, "sides": list(sides), "robot": self.robot, "version": self.version,
                      "simulated": self.simulated, "default": d0, "plan": plan(d0), "step": 0, "steps": [], "done": False}
            self._apply(c)
        self._run("读默认值", fn)

    def _apply(self, c) -> None:
        """把当前这一档设到夹爪上并回读。"""
        t = self.t
        v = t["plan"][t["step"]]
        ok = all(self._set(c, s, v) for s in t["sides"]) if v != t["default"] or t["step"] else True
        back = {s: self._get(c, s) for s in t["sides"]}
        t["steps"].append({"force": v, "set_ok": ok, "readback": back, "match": all(b == v for b in back.values()),
                           "photos": [], "note": "", "tau": None, "finished": False})
        if not ok or not t["steps"][-1]["match"]:
            self.error = f"夹持力没设成 {v:g}：回读 {back}"

    def add_photo(self, filename: str, data_b64: str) -> None:
        t = self.t
        if not t or t.get("done"):
            raise ValueError("没有在测")
        raw = base64.b64decode(data_b64.split(",", 1)[-1])
        if len(raw) > MAX_PHOTO:
            raise ValueError("照片超过 8 MB")
        ext = (Path(filename).suffix or ".jpg").lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".heic"):
            raise ValueError("只收图片：jpg、png、webp、heic")
        st = t["steps"][-1]
        folder = self._dir() / "img"
        folder.mkdir(exist_ok=True)
        p = folder / f"{t['name']}_{st['force']:g}_{len(st['photos']) + 1}{ext}"
        p.write_bytes(raw)
        st["photos"].append(p.name)

    def remove_photo(self, name: str) -> None:
        st = self.t["steps"][-1]
        if name in st["photos"]:
            st["photos"].remove(name)
            (self._dir() / "img" / name).unlink(missing_ok=True)

    def photo(self, name: str) -> Path | None:
        p = self._dir() / "img" / Path(name).name
        return p if p.exists() else None

    def finish_step(self, note: str) -> None:
        t = self.t
        if not t or t.get("done"):
            raise ValueError("没有在测")
        st = t["steps"][-1]
        if not st["photos"]:
            raise ValueError("先拍照上传：夹着杯子的样子至少一张")

        def fn(c):
            st["note"] = note.strip()
            st["tau"] = self._tau(c)
            st["finished"] = True
            if t["step"] + 1 < len(t["plan"]):
                t["step"] += 1
                self._apply(c)
            else:
                self._restore(c)
                self._save()
        self._run("切下一档", fn)

    def abort(self) -> None:
        if not self.t or self.t.get("done"):
            raise ValueError("没有在测")

        def fn(c):
            self._restore(c)
            self.t["aborted"] = True
            self._save()
        self._run("恢复默认值", fn)

    def _restore(self, c) -> None:
        t = self.t
        for s in t["sides"]:
            self._set(c, s, t["default"])
        t["restored"] = {s: self._get(c, s) for s in t["sides"]}
        t["done"] = True
        if any(v != t["default"] for v in t["restored"].values()):
            self.error = f"没恢复成默认值 {t['default']:g}：回读 {t['restored']}，手动设回去"

    def close(self) -> None:
        if self.t and self.t.get("done"):
            self.t = None

    def _save(self) -> None:
        with self.lock:
            rows = self.history()
            rows.append(dict(self.t, time=time.strftime("%H:%M:%S")))
            (self._dir() / "runs.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
            (self._dir() / "报告.md").write_text(markdown(rows), encoding="utf-8")

    def open_folder(self) -> str:
        import subprocess
        d = self._dir()
        subprocess.Popen(["xdg-open", str(d)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return str(d)


def markdown(rows: list[dict]) -> str:
    if not rows:
        return "还没有结果。\n"
    r0 = rows[-1]
    out = [f"# LimX 夹爪夹持力测试 {time.strftime('%Y-%m-%d')} {r0['robot'] or '—'}", "",
           f"机器人 {r0['robot'] or '—'}，版本 {r0['version'] or '—'}。每档设好后操作员用同一个特殊塑料杯夹住，拍照看形变。", ""]
    for r in rows:
        tag = "（中途放弃）" if r.get("aborted") else ""
        out += [f"## {r['time']} {r['side']}夹爪{tag}", "",
                f"默认值 {r['default']:g}；测完恢复回读 {r.get('restored', '—')}。", "",
                "| 档位 | 设定 | 回读 | 回读一致 | 夹爪指令 tau | 照片 | 备注 |", "|-|-|-|-|-|-|-|"]
        for k, s in enumerate(r["steps"]):
            lab = "默认" if k == 0 else f"{s['force']:g}"
            back = " / ".join(f"{v:g}" if v is not None else "—" for v in s["readback"].values())
            tau = " / ".join(f"{v:g}" for v in s["tau"]) if s.get("tau") else "没读到（不在跟随）"
            pics = " ".join(f"![{lab}](img/{p})" for p in s["photos"]) or "—"
            out.append(f"| {lab} | {s['force']:g} | {back} | {'是' if s['match'] else '否'} | {tau} | {pics} | {s['note'] or '—'} |")
        out.append("")
    out += ["## 方法", "",
            "- 设置 `/teleop/{left,right}_limx_gripper_force`（std_srvs/SetFloat32），回读 `/teleop/get_*_limx_gripper_force`。",
            "- 夹爪指令 tau 取点“已完成”时 `/limx/2F-gripper/cmd` 的一帧，顺序为左、右。",
            "- 设置只对本次运行有效，重启遥操服务后恢复 80。"]
    return "\n".join(out) + "\n"
