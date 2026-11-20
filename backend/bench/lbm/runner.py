"""VR部分动捕（LBMotion）摇杆跟随测试：给第一次用的人设计，只有“开始录包”和“结束并出报告”两个按钮。

流程：
1. 准备机器人（换机器人时自动做）：SSH 能登 → 把 bench/lbm 下四个分析脚本按 MD5 同步到主控 ~/Cyres/lbm/tools/lbmtools/
   → 主控有 mrosrecord → 磁盘剩余 ≥ 5 GB → 没有别的 mrosrecord 在跑。
2. 开始录包：主控上 setsid 后台起 mrosrecord（只录分析用的话题，不限时长，最长 MAX_RECORD_S 自动结束），
   同时 SSH 读 /cmd_vel，实时数推杆次数，告诉用户现在该做什么。mros 话题握手要约 10 s，收到第一帧才提示“可以推杆”。
3. 结束：给录包进程组发 SIGINT → 等 .bag 落盘 → MD5 → 主控上跑 analyze_bag → 每一次推杆存成一条记录
   → 按本场全部记录生成 Markdown 报告，存到 ~/.teleop-bench/lbm/<场次>/报告.md。
录包留在主控 ~/Cyres/lbm/bag/，需要留存时点“拷回录包”。
"""
from __future__ import annotations

import functools
import hashlib
import json
import math
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..db import data_dir
from ..robotstream import MROS, _json, echo_cmd
from . import analyze as an
from . import chart as ch
from . import script as sc

STICK = {"x": "左手摇杆前后", "y": "左手摇杆左右", "z": "右手摇杆左右"}
ITEMS = [
    {"key": "前进", "axis": "x", "sign": 1, "stick": "左手摇杆向前推满", "space": "前方留出 5 m 空地"},
    {"key": "后退", "axis": "x", "sign": -1, "stick": "左手摇杆向后拉满", "space": "后方留出 3 m 空地"},
    {"key": "左移", "axis": "y", "sign": 1, "stick": "左手摇杆向左推满", "space": "左侧留出 3 m 空地"},
    {"key": "右移", "axis": "y", "sign": -1, "stick": "左手摇杆向右推满", "space": "右侧留出 3 m 空地"},
    {"key": "左转", "axis": "z", "sign": 1, "stick": "右手摇杆向左推满", "space": "周围 1 m 内没有人和东西"},
    {"key": "右转", "axis": "z", "sign": -1, "stick": "右手摇杆向右推满", "space": "周围 1 m 内没有人和东西"},
]
ITEM = {i["key"]: i for i in ITEMS}
TOPICS = ("ImuData", "joint/state", "cmd_vel", "sdk_cmd_vel", "vr_cmd", "lbmotion_states", "joystick", "teleop/state", "teleop/session_state",
          "brainco2/hand/cmd", "brainco2/hand/state")
REMOTE = "~/Cyres/lbm"
TOOL_FILES = ("analyze.py", "leg_odom.py", "rosbag_lite.py", "hand.py")
HERE = Path(__file__).resolve().parent
TOOLS = HERE.parents[1] / "tools"
# 工作包：真人在 VR部分动捕下干活录的 /vr_cmd（pack.py make --half），回放看推杆跟随和灵巧手跟随
WORKPACKS = {
    "灵巧手取放杯": {"what": "工作包 198 s：低台前蹲下用灵巧手取放杯子，再转身走约 1.4 m 到航空箱前取放（11-20 在 660 上真人录）",
                 "space": "机器人四周留出 2 m 空地，天车绳长够骨盆往下 0.3 m（中途会下蹲）",
                 "file": HERE.parents[2] / "assets" / "lbm" / "灵巧手取放杯.bag"},
}
PUSH_S = 5.0          # 建议每次推满多久
SETTLE_S = 3.0        # 松杆后至少等多久再推下一次
MIN_PUSH_S = 0.5      # 短于它不算一次
MIN_FREE_GB = 5
MAX_RECORD_S = 15 * 60
CMD_ON = 0.05


def steps(item: str, reps: int) -> list[str]:
    i = ITEM[item]
    return [
        f"机器人挂好天车，{i['space']}，遥控器急停拿在手上。",
        "Pico 连上机器人、进入行走模式，机器人站稳。",
        "等弹窗显示“可以推杆”再动手。",
        f"{i['stick']}，保持 {PUSH_S:.0f} 秒（弹窗会计时）。",
        f"手指直接离开摇杆，让它自己回中，不要往回拉；等机器人站稳 {SETTLE_S:.0f} 秒。",
        f"回到起点再做一次，一共 {reps} 次。弹窗会数次数。",
        "做完点“结束并出报告”。",
    ]


# ---------------- 主控命令（纯函数，便于测试） ----------------
def record_cmd(name: str) -> str:
    keep = "|".join(re.escape(t) for t in TOPICS)
    n = shlex.quote(name)
    return (
        f"mkdir -p {REMOTE}/bag && cd {REMOTE}/bag && "
        "if pgrep -x mrosrecord >/dev/null; then echo BUSY; exit 3; fi; "
        f"{MROS}setsid nohup mrosrecord -q -O {n} -a -x {shlex.quote(f'^/(?!({keep})$).*')} > {n}.log 2>&1 < /dev/null & echo PID $!"
    )


def stop_cmd(name: str, pid: int) -> str:
    n = shlex.quote(name)
    return (
        f"cd {REMOTE}/bag; kill -INT -- -{int(pid)} 2>/dev/null; "
        f"for i in $(seq 1 60); do [ -f {n}.bag ] && [ ! -f {n}.bag.active ] && break; sleep 0.5; done; "
        f"[ -f {n}.bag ] || {{ echo NOBAG; exit 4; }}; md5sum {n}.bag | tee {n}.bag.md5; stat -c %s {n}.bag"
    )


def discard_cmd(name: str, pid: int) -> str:
    n = shlex.quote(name)
    return (f"cd {REMOTE}/bag; kill -INT -- -{int(pid)} 2>/dev/null; sleep 2; "
            f"rm -f {n}.bag {n}.bag.active {n}.bag.md5 {n}.log; echo OK")


def hands_cmd(name: str) -> str:
    n = shlex.quote(name)
    return (f"cd {REMOTE}/tools && python3 -c 'import json,sys; from lbmtools.hand import hands_bag; "
            f"print(\"JSON\", json.dumps(hands_bag(sys.argv[1]), ensure_ascii=False))' ../bag/{n}.bag")


@functools.lru_cache(maxsize=None)
def workpack_seconds(key: str) -> float:
    from . import rosbag_lite as rl
    _, first, last = rl.Bag(str(WORKPACKS[key]["file"])).summary()
    return round(max(last.values()) - min(first.values()), 1)


def analyze_cmd(name: str) -> str:
    n = shlex.quote(name)
    return (f"cd {REMOTE}/tools && python3 -c 'import json,sys; from lbmtools.analyze import analyze_bag; "
            f"print(\"JSON\", json.dumps(analyze_bag(sys.argv[1])))' ../bag/{n}.bag")


AUTO_TOOLS = {"pack.py": HERE.parent / "wbt" / "pack.py", "bag_extract.py": HERE / "bag_extract.py"}
HZ_S = 12            # mrostopic hz 要等多久：mros 话题握手 7 s 以上才出第一个数
PLAY_LEAD = 3.0      # 开始录包后多久开始播脚本


def pose_cmd(value: int | None = None) -> str:
    """读（value=None）或设遥操预选模式：0 = VR部分动捕，2 = VR全身动捕。"""
    if value is None:
        return f"{MROS}timeout 15 mrosservice call /teleop/get_pose_ctrl_mode std_srvs/GetInt32 '{{}}' 2>&1 | grep -m1 response || true"
    return (f"{MROS}timeout 15 mrosservice call /teleop/pose_ctrl_mode std_srvs/SetInt32 "
            f"'{{\"data\":{int(value)}}}' 2>&1 | grep -m1 response || true")


def script_play_cmd(name: str, pack: str) -> str:
    return (f"cd {REMOTE}/tools/lbmtools && {MROS}setsid nohup python3 pack.py play ../../packs/{shlex.quote(pack)} "
            f"> ../../bag/{shlex.quote(name)}.play.log 2>&1 < /dev/null & echo PID $!")


def _response(out: str) -> dict:
    m = re.search(r"response:\s*(\{.*\})", out)
    return json.loads(m.group(1)) if m else {}


def local_tool_md5() -> dict[str, str]:
    return {f: hashlib.md5((HERE / f).read_bytes()).hexdigest() for f in TOOL_FILES}


def _pick(segments: list[dict], item: str) -> dict | None:
    mine = [s for s in segments if s["kind"] == item and not s["cut_start"]]
    return max(mine, key=lambda s: s["push_s"]) if mine else None


# ---------------- 实时数推杆 ----------------
class PushCounter:
    """按 /cmd_vel 数推杆：主方向同号且 |值| > CMD_ON 算推着，连续 ≥ MIN_PUSH_S 才算一次；
    别的方向推满 MIN_PUSH_S 记一次“推错方向”。时间用电脑收到数据的时间。"""

    def __init__(self, item: str):
        self.axis = {"x": 0, "y": 1, "z": 2}[ITEM[item]["axis"]]
        self.sign = ITEM[item]["sign"]
        self.pushes: list[float] = []
        self.wrong = 0
        self.on_since: float | None = None
        self.other_since: float | None = None
        self.off_since: float | None = None
        self.first: float | None = None
        self.last: float | None = None

    def feed(self, t: float, v: tuple[float, float, float]) -> None:
        self.first = t if self.first is None else self.first
        self.last = t
        mine = v[self.axis] * self.sign > CMD_ON
        other = not mine and max(abs(x) for x in v) > CMD_ON
        if mine and self.on_since is None:
            self.on_since = t
        if not mine and self.on_since is not None:
            if t - self.on_since >= MIN_PUSH_S:
                self.pushes.append(round(t - self.on_since, 1))
                self.off_since = t
            self.on_since = None
        if other and self.other_since is None:
            self.other_since = t
        if not other and self.other_since is not None:
            if t - self.other_since >= MIN_PUSH_S:
                self.wrong += 1
            self.other_since = None

    def status(self, now: float) -> dict:
        base = {"pushing_s": 0, "settle_left": 0}
        if self.first is None:
            return {**base, "phase": "准备", "text": "正在连接指令流（约 10 秒），先别推杆"}
        if now - self.last > 3:
            return {**base, "phase": "断流", "text": "3 秒没收到 /cmd_vel：检查热点和 Pico 连接，录包还在继续"}
        if self.on_since is not None:
            d = now - self.on_since
            left = PUSH_S - d
            return {**base, "phase": "推杆", "pushing_s": round(d, 1),
                    "text": f"推着，再保持 {math.ceil(left)} 秒" if left > 0 else "可以松手了"}
        if self.other_since is not None:
            return {**base, "phase": "方向不对", "text": "推的方向不对，松手"}
        if self.off_since is not None and now - self.off_since < SETTLE_S:
            left = SETTLE_S - (now - self.off_since)
            return {**base, "phase": "等停", "settle_left": round(left, 1), "text": f"松手了，等机器人站稳，{math.ceil(left)} 秒后再推"}
        return {**base, "phase": "可以推杆", "text": "可以推杆"}


def parse_twist(line: str) -> tuple[float, float, float] | None:
    d = _json(line)
    if not d or "linear" not in d:
        return None
    return float(d["linear"]["x"]), float(d["linear"]["y"]), float(d["angular"]["z"])


# ---------------- 演练：不连机器人 ----------------
def simulated_series(item: str, pushes: list[tuple[float, float]], end: float, seed: float = 0.0) -> an.Series:
    it = ITEM[item]
    s = an.Series()
    dt, x, y, yaw, v = 0.02, 0.0, 0.0, 0.0, 0.0
    gain = {"x": 0.6, "y": 0.5, "z": 0.64}[it["axis"]]
    full = {"x": 1.0 if it["sign"] > 0 else 0.5, "y": 0.8, "z": 0.8}[it["axis"]]
    drift = 0.02 + 0.01 * math.sin(seed)
    for k in range(int(end / dt)):
        t = k * dt
        on = any(a <= t < b for a, b in pushes)
        c = it["sign"] * full if on else 0.0
        if k % 2 == 0:
            s.cmd.append((t, c if it["axis"] == "x" else 0.0, c if it["axis"] == "y" else 0.0, c if it["axis"] == "z" else 0.0))
        internal = c * (0.5 if it["axis"] == "x" else 0.75)
        s.lbm.append((t, internal if it["axis"] == "x" else 0.0, internal if it["axis"] == "z" else 0.0))
        v += (c * gain - v) * min(1.0, dt / 0.4)
        if it["axis"] == "z":
            yaw += v * dt
        else:
            vx, vy = (v, 0.0) if it["axis"] == "x" else (0.0, v)
            vy += drift * abs(v)
            yaw += drift * v * dt
            x += (vx * math.cos(yaw) - vy * math.sin(yaw)) * dt
            y += (vx * math.sin(yaw) + vy * math.cos(yaw)) * dt
        s.yaw.append((t, yaw))
        s.pos.append((t, x, y))
        step = abs(v) > 0.05 and (k // 25) % 2 == 0
        s.knee.append((t, 0.6 + (0.4 if step else 0.0), 0.6 + (0.4 if abs(v) > 0.05 and not step else 0.0)))
    return s


@dataclass
class Take:
    item: str
    reps: int
    name: str
    started: float
    pid: int = 0
    counter: PushCounter | None = None
    stop_evt: threading.Event = field(default_factory=threading.Event)
    client: object = None
    sim_pushes: list = field(default_factory=list)


class LbmController:
    def __init__(self, ssh_factory=None):
        self._ssh_factory = ssh_factory
        self.robot = self.version = self.battery = self.status = ""
        self.simulated = False
        self.lock = threading.Lock()
        self.prep: dict = {"robot": "", "running": False, "checks": [], "ok": False}
        self.take: Take | None = None
        self._auto_stop = threading.Event()
        self.finish: dict | None = None   # 结束流程的进度和结果，弹窗用
        self.fetching = ""
        self.error = ""

    # ---------- 会话和存储 ----------
    def bind(self, *, robot: str, version: str, simulated: bool, battery: str = "", status: str = "") -> None:
        self.robot, self.version, self.simulated, self.battery, self.status = robot, version, simulated, battery, status
        if not self.take and self.prep.get("robot") != (robot, simulated):
            self.prepare()

    @property
    def session(self) -> str:
        return ("演练 " if self.simulated else "") + time.strftime("%Y-%m-%d") + " " + (self.robot or "未识别")

    def _dir(self) -> Path:
        d = data_dir() / "lbm" / self.session.replace(" ", "_")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _load(self, name: str) -> list[dict]:
        f = self._dir() / name
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []

    def _store(self, name: str, rows: list[dict]) -> None:
        (self._dir() / name).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    def trials(self) -> list[dict]:
        rows = self._load("trials.json")
        if any("plan" in r for r in rows):
            rows = self._migrate(rows)
        # 早先的记录没有 aborted 字段，按所属录包的状态补上，没确认过的当作待确认
        stopped = {k["id"] for k in self.takes() if k.get("status") == "中途停止"}
        for r in rows:
            if "aborted" not in r and r.get("take") in stopped:
                r.update(aborted=True, ask=True)
        return rows

    def _migrate(self, rows: list[dict]) -> list[dict]:
        """11-10 第一版一趟一条、可能没有结果；没结果的挪到录包列表，有结果的补上 take 字段。"""
        with self.lock:
            takes = self._load("takes.json")
            nxt = max([k["id"] for k in takes], default=0)
            keep = []
            for r in rows:
                if "plan" not in r:
                    keep.append(r)
                    continue
                nxt += 1
                takes.append({"id": nxt, "item": r["item"], "reps": 1, "name": r["name"], "started": r["started"],
                              "ended": r["started"], "bag": r.get("bag", ""), "md5": r.get("md5", ""), "size": r.get("size", 0),
                              "status": "完成" if r.get("main") else "没有有效推杆", "n_found": 1 if r.get("main") else 0,
                              "n_other": 0, "note": r.get("note", "")})
                if r.get("main"):
                    keep.append({k: v for k, v in r.items() if k not in ("plan", "segments")} | {"take": nxt, "segments": []})
            self._store("takes.json", takes)
            self._store("trials.json", keep)
        return keep

    def takes(self) -> list[dict]:
        return self._load("takes.json")

    def _append(self, name: str, rows: list[dict]) -> list[dict]:
        with self.lock:
            cur = self._load(name)
            nxt = max([r["id"] for r in cur], default=0)
            for r in rows:
                nxt += 1
                r["id"] = nxt
            self._store(name, cur + rows)
        return rows

    def _update(self, name: str, rid: int, **kw) -> None:
        with self.lock:
            cur = self._load(name)
            for r in cur:
                if r["id"] == rid:
                    r.update(kw)
            self._store(name, cur)

    def delete(self, tid: int) -> bool:
        with self.lock:
            cur = self.trials()
            keep = [t for t in cur if t["id"] != tid]
            self._store("trials.json", keep)
        return len(keep) != len(cur)

    def exclude(self, tid: int, excluded: bool, note: str = "") -> None:
        self._update("trials.json", tid, excluded=excluded, exclude_note=note)

    def keep_aborted(self, take: int, keep: bool) -> None:
        """中途停止那次录包里的推杆要不要计入报告。不计入只是不进汇总，录包和记录都留着。"""
        for t in self.trials():
            if t["take"] == take and t.get("ask"):
                self._update("trials.json", t["id"], aborted=True, ask=False, excluded=not keep,
                             exclude_note="" if keep else "中途停止，选择不计入报告")

    # ---------- 状态 ----------
    def state(self) -> dict:
        trials = self.trials()
        mains = [t["main"] for t in trials if t.get("main") and not t.get("excluded") and not ch.no_lbm(t) and not t.get("workpack")]
        tk = self.take
        live = None
        if tk:
            now = time.time()
            c = tk.counter
            st = c.status(now) if c else {"phase": "准备", "text": "", "pushing_s": 0, "settle_left": 0}
            live = {"item": tk.item, "reps": tk.reps, "name": tk.name, "elapsed": round(now - tk.started, 1),
                    "pushes": c.pushes if c else [], "wrong": c.wrong if c else 0, **st,
                    "steps": steps(tk.item, tk.reps), "max_s": MAX_RECORD_S}
        return {
            "session": self.session, "robot": self.robot, "version": self.version, "simulated": self.simulated,
            "items": [dict(i, steps=steps(i["key"], 5)) for i in ITEMS], "push_s": PUSH_S,
            "scripts": [{"key": k, "what": v["what"], "space": v["space"], "kinds": [m[0] for m in v["moves"]],
                         "fixed_s": round(sc.timeline(k, 1)[1] - sum(m[4] + sc.SETTLE for m in v["moves"])),
                         "per_rep": round(sum(m[4] + sc.SETTLE for m in v["moves"]))}
                        for k, v in sc.SCRIPTS.items()],
            "workpacks": [{"key": k, "what": v["what"], "space": v["space"], "seconds": workpack_seconds(k)} for k, v in WORKPACKS.items()],
            "prep": self.prep, "live": live, "finish": self.finish, "error": self.error, "fetching": self.fetching,
            "trials": trials, "takes": self.takes(), "summary": an.summarize(mains),
            "report_path": str(self._dir() / "报告.md") if (self._dir() / "报告.md").exists() else "",
        }

    # ---------- 1. 准备机器人 ----------
    def prepare(self, force: bool = False) -> None:
        if self.prep.get("running"):
            return
        if not force and self.prep.get("robot") == (self.robot, self.simulated) and self.prep.get("ok"):
            return
        self.prep = {"robot": (self.robot, self.simulated), "running": True, "checks": [], "ok": False}
        threading.Thread(target=self._prepare, daemon=True).start()

    def _check(self, name: str, ok: bool | None, detail: str = "") -> None:
        cs = [c for c in self.prep["checks"] if c["name"] != name]
        cs.append({"name": name, "ok": ok, "detail": detail})
        self.prep["checks"] = cs

    def _prepare(self) -> None:
        p = self.prep
        try:
            if self.simulated:
                for n in ("连接机器人", "分析脚本", "录包工具", "磁盘空间"):
                    self._check(n, True, "演练")
                p["ok"] = True
                return
            if not self.robot:
                self._check("连接机器人", False, "没识别到机器人：连上机器人热点")
                return
            self._check("连接机器人", None, "登录主控…")
            try:
                c = self._ssh_factory()
            except Exception as exc:  # noqa: BLE001
                self._check("连接机器人", False, f"SSH 登不上主控 1.2：{exc}")
                return
            try:
                self._check("连接机器人", True, f"{self.robot}，{self.version or '版本未读到'}"
                            + (f"，电量 {self.battery}" if self.battery else ""))
                self._check("分析脚本", None, "核对…")
                n = self._sync_tools(c)
                self._check("分析脚本", True, f"已更新 {n} 个文件" if n else "已是最新")
                out = self._exec(c, f"{MROS}which mrosrecord; df -BG --output=avail ~ | tail -1; pgrep -x mrosrecord || true")
                lines = [l.strip() for l in out.splitlines() if l.strip()]
                has = any(l.endswith("mrosrecord") for l in lines)
                self._check("录包工具", has, "mrosrecord 可用" if has else "主控上没有 mrosrecord")
                gb = next((int(l.rstrip("G")) for l in lines if re.fullmatch(r"\d+G", l)), 0)
                self._check("磁盘空间", gb >= MIN_FREE_GB, f"剩余 {gb} GB" + ("" if gb >= MIN_FREE_GB else f"，少于 {MIN_FREE_GB} GB"))
                busy = [l for l in lines if l.isdigit()]
                if busy:
                    self._check("没有别的录包", False, f"主控上已有 mrosrecord 在跑（pid {' '.join(busy)}），先停掉")
                p["ok"] = has and gb >= MIN_FREE_GB and not busy
            finally:
                c.close()
        except Exception as exc:  # noqa: BLE001
            self._check("准备", False, str(exc))
        finally:
            p["running"] = False

    def _sync_tools(self, c) -> int:
        want = local_tool_md5()
        out = self._exec(c, f"mkdir -p {REMOTE}/tools/lbmtools && cd {REMOTE}/tools/lbmtools && touch __init__.py && "
                            f"md5sum {' '.join(TOOL_FILES)} 2>/dev/null || true")
        have = {l.split()[1]: l.split()[0] for l in out.splitlines() if len(l.split()) == 2}
        todo = [f for f in TOOL_FILES if have.get(f) != want[f]]
        if todo:
            home = self._exec(c, "echo $HOME").strip()
            sftp = c.open_sftp()
            try:
                for f in todo:
                    sftp.put(str(HERE / f), f"{home}/Cyres/lbm/tools/lbmtools/{f}")
            finally:
                sftp.close()
        return len(todo)

    # ---------- 2. 开始录包 ----------
    def begin(self, item: str, reps: int = 5) -> None:
        if item not in ITEM:
            raise ValueError("没有这个测试项")
        if not 1 <= reps <= 20:
            raise ValueError("次数 1–20")
        if self.take:
            raise ValueError("已经在录了")
        if self.finish and self.finish.get("running"):
            raise ValueError("上一次还在出报告")
        if not self.prep.get("ok"):
            raise ValueError("机器人还没准备好：看上面的检查清单")
        name = f"{time.strftime('%m%d_%H%M%S')}_{item}"
        tk = Take(item=item, reps=reps, name=name, started=time.time(), counter=PushCounter(item))
        self.error, self.finish = "", None
        if not self.simulated:
            c = self._ssh_factory()
            try:
                out = self._exec(c, record_cmd(name), timeout=20)
            except RuntimeError as exc:
                c.close()
                raise RuntimeError("主控上已有 mrosrecord 在录，先停掉它" if "BUSY" in str(exc) else str(exc)) from None
            m = re.search(r"PID (\d+)", out)
            if not m:
                c.close()
                raise RuntimeError(f"录包没起来：{out.strip()[-200:]}")
            tk.pid = int(m.group(1))
            tk.client = c
        self.take = tk
        threading.Thread(target=self._watch, args=(tk,), daemon=True).start()

    def _watch(self, tk: Take) -> None:
        """实时读 /cmd_vel 数推杆；演练按固定节拍造推杆。超过 MAX_RECORD_S 自动结束。"""
        if self.simulated:
            t0 = time.time()
            tk.stop_evt.wait(2)
            while not tk.stop_evt.is_set():
                t = time.time() - t0
                cyc = (t - 2) % (PUSH_S + SETTLE_S + 2)
                on = len(tk.counter.pushes) < tk.reps and 0 <= cyc < PUSH_S
                if on and (not tk.sim_pushes or tk.sim_pushes[-1][1] is not None):
                    tk.sim_pushes.append([t, None])
                if not on and tk.sim_pushes and tk.sim_pushes[-1][1] is None:
                    tk.sim_pushes[-1][1] = t
                v = [0.0, 0.0, 0.0]
                if on:
                    v[tk.counter.axis] = 0.8 * tk.counter.sign
                tk.counter.feed(time.time(), tuple(v))
                if time.time() - tk.started > MAX_RECORD_S:
                    break
                tk.stop_evt.wait(0.05)
        else:
            try:
                _, o, _ = tk.client.exec_command(echo_cmd("/cmd_vel", 1), get_pty=True)
                for line in iter(o.readline, ""):
                    if tk.stop_evt.is_set():
                        break
                    v = parse_twist(line)
                    if v:
                        tk.counter.feed(time.time(), v)
                    if time.time() - tk.started > MAX_RECORD_S:
                        break
            except Exception as exc:  # noqa: BLE001
                if not tk.stop_evt.is_set():
                    self.error = f"读 /cmd_vel 中断：{exc.__class__.__name__}。录包还在继续，可以照常结束"
        if not tk.stop_evt.is_set() and self.take is tk:
            self.error = f"录了 {MAX_RECORD_S // 60} 分钟，自动结束"
            self.end()

    # ---------- 3. 结束 ----------
    def end(self) -> None:
        tk = self.take
        if not tk:
            raise ValueError("没有在录")
        tk.stop_evt.set()
        self.finish = {"running": True, "item": tk.item, "name": tk.name,
                       "stages": [{"name": n, "state": "wait"} for n in ("停止录包", "校验 MD5", "主控分析", "生成报告")],
                       "result": None, "report": "", "error": ""}
        threading.Thread(target=self._end, args=(tk,), daemon=True).start()

    def _stage(self, name: str, state: str, detail: str = "") -> None:
        for s in self.finish["stages"]:
            if s["name"] == name:
                s["state"], s["detail"] = state, detail

    def _end(self, tk: Take) -> None:
        f = self.finish
        take = {"item": tk.item, "reps": tk.reps, "name": tk.name, "started": tk.started, "ended": time.time(),
                "bag": "", "md5": "", "size": 0, "status": "", "note": "", "live_pushes": list(tk.counter.pushes)}
        cur = "停止录包"
        try:
            self._stage(cur, "run")
            if self.simulated:
                if tk.sim_pushes and tk.sim_pushes[-1][1] is None:
                    tk.sim_pushes[-1][1] = time.time() - tk.started
                end = time.time() - tk.started + 3
                self._stage(cur, "ok")
                for cur in ("校验 MD5", "主控分析"):
                    self._stage(cur, "ok", "演练")
                segs = an.analyze(simulated_series(tk.item, [tuple(p) for p in tk.sim_pushes], end + SETTLE_S, seed=tk.started))
            else:
                c = tk.client
                try:
                    out = self._exec(c, stop_cmd(tk.name, tk.pid), timeout=60).split()
                    self._stage(cur, "ok")
                    cur = "校验 MD5"
                    take.update(bag=f"{REMOTE}/bag/{tk.name}.bag", md5=out[0], size=int(out[-1]))
                    self._stage(cur, "ok", f"{out[0][:8]}…，{int(out[-1]) / 1e6:.0f} MB")
                    cur = "主控分析"
                    self._stage(cur, "run")
                    self._sync_tools(c)
                    res = self._exec(c, analyze_cmd(tk.name), timeout=600)
                finally:
                    c.close()
                line = next((l for l in res.splitlines() if l.startswith("JSON ")), "")
                if not line:
                    raise RuntimeError(f"主控分析没有输出：{res.strip()[-300:]}")
                segs = json.loads(line[5:])
                if wp:
                    hl = next((ln for ln in self._exec(c, hands_cmd(name), timeout=300).splitlines() if ln.startswith("JSON ")), "")
                    take["hands"] = json.loads(hl[5:]) if hl else {}
            mine = [s for s in segs if s["kind"] == tk.item and not s["cut_start"]]
            other = len(segs) - len(mine)
            self._stage("主控分析", "ok", f"找到 {len(mine)} 次{tk.item}" + (f"，另有 {other} 段其他方向的推杆不计" if other else ""))
            cur = "生成报告"
            self._stage(cur, "run")
            take.update(status="完成" if mine else "没有有效推杆", n_found=len(mine), n_other=other,
                        note="" if mine else f"录包里没有“{tk.item}”方向、推满 {MIN_PUSH_S} s 以上并松杆的推杆")
            tkrow = self._append("takes.json", [take])[0]
            rows = self._append("trials.json", [{"take": tkrow["id"], "item": tk.item, "name": tk.name,
                                                  "started": tk.started + s["t"], "bag": take["bag"], "md5": take["md5"],
                                                  "status": "完成", "main": s, "segments": []} for s in mine])
            path, lint = self.write_report()
            self._stage(cur, "ok", f"写作规范检查：错误 {lint['errors']}")
            f["result"] = {"take": tkrow["id"], "trials": [r["id"] for r in rows], "found": len(mine), "reps": tk.reps,
                           "summary": an.summarize([r["main"] for r in rows])}
            f["report"] = path
        except Exception as exc:  # noqa: BLE001
            self._stage(cur, "fail", str(exc))
            f["error"] = f"{cur}失败：{exc}"
            if not take.get("status"):
                take.update(status="失败", note=f["error"])
                self._append("takes.json", [take])
        finally:
            f["running"] = False
            self.take = None

    def discard(self) -> None:
        tk = self.take
        if not tk:
            raise ValueError("没有在录")
        tk.stop_evt.set()
        if not self.simulated:
            try:
                self._exec(tk.client, discard_cmd(tk.name, tk.pid), timeout=30)
            finally:
                tk.client.close()
        self.take = None
        self.finish = None

    def close_finish(self) -> None:
        if self.finish and not self.finish.get("running"):
            self.finish = None


    # ---------- 自动推杆（软件生成摇杆脚本，不用人拿 Pico） ----------
    def auto_begin(self, script: str, reps: int = 3) -> None:
        if script in WORKPACKS:
            reps = 1
        elif script not in sc.SCRIPTS:
            raise ValueError("没有这个脚本")
        if not 1 <= reps <= 5:
            raise ValueError("次数 1–5")
        if self.take or (self.finish and self.finish.get("running")):
            raise ValueError("上一次还没结束")
        if not self.prep.get("ok"):
            raise ValueError("机器人还没准备好：看左边的检查清单")
        if not self.simulated and self.status and self.status != "Walk":
            raise ValueError(f"机器人现在是 {self.status}，先用遥控器切到 Walk 站稳")
        name = f"{time.strftime('%m%d_%H%M%S')}_自动{script}"
        self._auto_stop.clear()
        self.error = ""
        self.finish = {"running": True, "auto": True, "item": script, "name": name, "result": None, "report": "", "error": "",
                       "elapsed": 0.0, "total": workpack_seconds(script) if script in WORKPACKS else sc.timeline(script, reps)[1],
                       "stages": [{"name": n, "state": "wait"} for n in
                                  ("检查", "切到 VR部分动捕", "录包", "播工作包" if script in WORKPACKS else "自动推杆",
                                   "停止录包", "主控分析", "生成报告", "恢复设置")]}
        threading.Thread(target=self._auto, args=(script, reps, name), daemon=True).start()

    def auto_stop(self) -> None:
        if not (self.finish and self.finish.get("auto") and self.finish.get("running")):
            raise ValueError("没有在自动推杆")
        self._auto_stop.set()

    def _auto(self, script: str, reps: int, name: str) -> None:
        f = self.finish
        wp = WORKPACKS.get(script)
        kinds = [i["key"] for i in ITEMS] if wp else [m[0] for m in sc.SCRIPTS[script]["moves"]]
        total = workpack_seconds(script) if wp else sc.timeline(script, reps)[1]
        plan = [] if wp else sc.pushes(script, reps)
        take = {"item": script, "reps": reps, "name": name, "started": time.time(), "ended": 0, "bag": "", "md5": "",
                "size": 0, "status": "", "note": "工作包回放" if wp else "软件自动推杆", "auto": True}
        cur, c, rec_pid, old_mode, aborted = "检查", None, None, None, False
        try:
            self._stage(cur, "run")
            if self.simulated:
                for cur in ("检查", "切到 VR部分动捕", "录包"):
                    self._stage(cur, "ok", "演练")
                cur = "播工作包" if wp else "自动推杆"
                t0 = time.time()
                while time.time() - t0 < min(total, 8) and not self._auto_stop.is_set():
                    f["elapsed"] = el = (time.time() - t0) / min(total, 8) * total
                    nxt = next((p for p in plan if p["end"] > el), None)
                    what = "正在播工作包" if wp else (f"正在{nxt['kind']}" if nxt and nxt["start"] <= el else (f"{nxt['start'] - el:.0f} 秒后{nxt['kind']}" if nxt else "收尾，回 Walk"))
                    self._stage(cur, "run", f"演练：{what}")
                    time.sleep(0.2)
                aborted = self._auto_stop.is_set()
                if not aborted:
                    f["elapsed"] = total
                self._stage(cur, "fail" if aborted else "ok", "演练：中途停止" if aborted else "演练")
                self._stage("停止录包", "ok", "演练")
                segs = []
                for k in ([] if wp else kinds):
                    ps = [(p["start"], p["end"]) for p in plan if p["kind"] == k and p["end"] <= f["elapsed"]]
                    segs += an.analyze(simulated_series(k, ps, total + 2, seed=t0))
            else:
                c = self._ssh_factory()
                hz = self._exec(c, f"{MROS}timeout {HZ_S} mrostopic hz /vr_cmd 2>&1 | grep -m1 'average rate' || true", timeout=HZ_S + 15)
                if "average" in hz:
                    raise RuntimeError("/vr_cmd 还有别的发布者：关掉 Pico 或退出遥操 App")
                old_mode = _response(self._exec(c, pose_cmd())).get("data")
                if old_mode is None:
                    raise RuntimeError("读不到遥操预选模式（/teleop/get_pose_ctrl_mode 没响应）：遥操节点在跑吗")
                if wp:
                    local = wp["file"]
                else:
                    local = data_dir() / "lbm" / "scripts" / f"{script}_x{reps}.bag"
                    local.parent.mkdir(parents=True, exist_ok=True)
                    if not local.exists():
                        sc.make(script, reps, local)
                self._sync_tools(c)
                self._sync_auto(c, local)
                self._stage(cur, "ok", "Pico 已断开，" + ("工作包已上传" if wp else "脚本已上传"))
                cur = "切到 VR部分动捕"
                self._stage(cur, "run")
                if old_mode != 0:
                    _response(self._exec(c, pose_cmd(0)))
                now = _response(self._exec(c, pose_cmd())).get("data")
                if now != 0:
                    raise RuntimeError(f"预选模式没切成 VR部分动捕（读回 {now}）")
                self._stage(cur, "ok", f"预选模式 {old_mode} → 0，测完恢复" if old_mode != 0 else "已经是 VR部分动捕")
                cur = "录包"
                self._stage(cur, "run")
                out = self._exec(c, record_cmd(name), timeout=20)
                if "BUSY" in out:
                    raise RuntimeError("主控上已有 mrosrecord 在录，先停掉它")
                rec_pid = int(re.search(r"PID (\d+)", out).group(1))
                self._auto_stop.wait(PLAY_LEAD)
                self._stage(cur, "ok")
                cur = "播工作包" if wp else "自动推杆"
                if self._auto_stop.is_set():
                    raise RuntimeError("已停止，没有开始推杆")
                self._stage(cur, "run", "机器人会自己进 VR部分动捕" + ("，照录好的动作走和操作" if wp else "并走起来") + "，手放在遥控器急停上")
                play_pid = int(re.search(r"PID (\d+)", self._exec(c, script_play_cmd(name, local.name), timeout=20)).group(1))
                t0 = time.time()
                while self._exec(c, f"kill -0 {play_pid} 2>/dev/null && echo RUN || echo END").strip() == "RUN":
                    el = time.time() - t0
                    nxt = next((p for p in plan if p["end"] > el), None)
                    what = "正在播工作包，机器人照录好的动作做" if wp else (
                        f"正在{nxt['kind']}" if nxt and nxt["start"] <= el else (f"{nxt['start'] - el:.0f} 秒后{nxt['kind']}" if nxt else "收尾，回 Walk"))
                    f["elapsed"] = el
                    self._stage(cur, "run", what)
                    if self._auto_stop.is_set() or el > total + 30:
                        self._exec(c, f"kill -INT -- -{play_pid} 2>/dev/null; kill -INT {play_pid} 2>/dev/null; true")
                        aborted = True
                        break
                    time.sleep(0.5)
                if not aborted:
                    f["elapsed"] = total
                self._stage(cur, "ok" if not aborted else "fail", "中途停止：机器人可能还在遥操里，用遥控器切回 Walk" if aborted else "播完")
                cur = "停止录包"
                self._stage(cur, "run")
                time.sleep(2)
                out = self._exec(c, stop_cmd(name, rec_pid), timeout=60).split()
                rec_pid = None
                take.update(bag=f"{REMOTE}/bag/{name}.bag", md5=out[0], size=int(out[-1]))
                self._stage(cur, "ok", f"{int(out[-1]) / 1e6:.0f} MB")
                cur = "主控分析"
                self._stage(cur, "run")
                res = self._exec(c, analyze_cmd(name), timeout=600)
                line = next((ln for ln in res.splitlines() if ln.startswith("JSON ")), "")
                if not line:
                    raise RuntimeError(f"主控分析没有输出：{res.strip()[-300:]}")
                segs = json.loads(line[5:])
            mine = [sg for sg in segs if sg["kind"] in kinds and not sg["cut_start"]]
            counts = {k: sum(sg["kind"] == k for sg in mine) for k in kinds}
            if wp:
                hs = (take.get("hands") or {}).get("手") or {}
                self._stage("主控分析", "ok", "，".join([f"{k} {n} 次" for k, n in counts.items() if n] +
                                                       [f"{k}开合 {v.get('指令开合次数', 0)} 次" for k, v in hs.items()]) or "没有推杆")
            else:
                self._stage("主控分析", "ok", "，".join(f"{k} {n}/{reps} 次" for k, n in counts.items()))
            cur = "生成报告"
            self._stage(cur, "run")
            take.update(ended=time.time(), status=("中途停止" if aborted else "完成") if (mine or wp) else "没有有效推杆",
                        n_found=len(mine), n_other=len(segs) - len(mine))
            if not mine and not wp:
                take["note"] += "；录包里没找到推杆：看 /teleop/state 有没有进跟随、/cmd_vel 有没有速度"
            tkrow = self._append("takes.json", [take])[0]
            rows = self._append("trials.json", [{"take": tkrow["id"], "item": sg["kind"], "name": name, "started": take["started"] + sg["t"],
                                                  "bag": take["bag"], "md5": take["md5"], "status": "完成", "auto": True,
                                                  "aborted": aborted, "ask": aborted, "workpack": script if wp else "",
                                                  "main": sg, "segments": []} for sg in mine])
            path, lint = self.write_report()
            self._stage(cur, "ok", f"写作规范检查：错误 {lint['errors']}")
            f["result"] = {"take": tkrow["id"], "trials": [r["id"] for r in rows], "found": len(mine),
                           "reps": len(mine) if wp else reps * len(kinds), "hands": (take.get("hands") or {}).get("手"), "workpack": bool(wp),
                           "summary": an.summarize([r["main"] for r in rows])}
            f["report"] = path
            if aborted and not f["error"]:
                f["error"] = "中途停止了，结果只含停止前的推杆"
        except Exception as exc:  # noqa: BLE001
            self._stage(cur, "fail", str(exc))
            f["error"] = f"{cur}失败：{exc}"
            if c and rec_pid:
                try:
                    self._exec(c, stop_cmd(name, rec_pid), timeout=60)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            if c:
                try:
                    if old_mode is not None and old_mode != 0:
                        self._stage("恢复设置", "run")
                        _response(self._exec(c, pose_cmd(old_mode)))
                        back = _response(self._exec(c, pose_cmd())).get("data")
                        self._stage("恢复设置", "ok" if back == old_mode else "fail", f"预选模式恢复成 {back}")
                    else:
                        self._stage("恢复设置", "ok", "没改过设置")
                except Exception as exc:  # noqa: BLE001
                    self._stage("恢复设置", "fail", f"预选模式没恢复：{exc}")
                c.close()
            else:
                self._stage("恢复设置", "ok", "演练" if self.simulated else "没改过设置")
            f["running"] = False

    def _sync_auto(self, c, pack: Path) -> None:
        files = {f"tools/lbmtools/{k}": v for k, v in AUTO_TOOLS.items()} | {f"packs/{pack.name}": pack}
        want = {k: hashlib.md5(v.read_bytes()).hexdigest() for k, v in files.items()}
        out = self._exec(c, f"mkdir -p {REMOTE}/packs && cd {REMOTE} && md5sum {' '.join(shlex.quote(k) for k in files)} 2>/dev/null || true")
        have = {ln.split()[1]: ln.split()[0] for ln in out.splitlines() if len(ln.split()) == 2}
        todo = [k for k in files if have.get(k) != want[k]]
        if todo:
            home = self._exec(c, "echo $HOME").strip()
            sftp = c.open_sftp()
            try:
                for k in todo:
                    sftp.put(str(files[k]), f"{home}/Cyres/lbm/{k}")
            finally:
                sftp.close()

    # ---------- 报告 ----------
    def write_report(self) -> tuple[str, dict]:
        img = self._dir() / "img" / "链路.png"
        img.parent.mkdir(parents=True, exist_ok=True)
        try:
            drawn = ch.chain(self.trials(), img, f"VR部分动捕摇杆跟随：指令 → LBMotion 内部 → 实际（{self.session}）")
        except Exception:  # 画图失败不影响出文字报告
            drawn = False
        if not drawn:
            img.unlink(missing_ok=True)
        md = self.markdown()
        p = self._dir() / "报告.md"
        p.write_text(md, encoding="utf-8")
        r = subprocess.run([sys.executable, str(TOOLS / "lint_report_style.py"), "--file", str(p)],
                           capture_output=True, text=True, timeout=60)
        m = re.search(r"错误 (\d+)，警告 (\d+)", r.stdout)
        return str(p), {"errors": int(m.group(1)) if m else -1, "warnings": int(m.group(2)) if m else -1}

    def markdown(self) -> str:
        st = self.state()
        rows = st["summary"]
        if not st["trials"]:
            return "还没有有效记录。\n"

        def f(v, unit="", pct=False, n=2):
            if not v:
                return "—"
            if pct:
                a, b, c = (round(x * 100, 1) for x in (v["median"], v["min"], v["max"]))
                return f"{a}%（{b}–{c}%，n={v['n']}）"
            return f"{v['median']:.{n}f}{unit}（{v['min']:.{n}f}–{v['max']:.{n}f}，n={v['n']}）"

        out = [f"# VR部分动捕摇杆跟随测试 {st['session']}", "",
               f"机器人 {st['robot'] or '—'}，版本 {st['version'] or '—'}。", "",
               "## 怎么看", "",
               "摇杆推出去后，速度要经过三级才变成机器人的动作：指令 → LBMotion 内部 → 实际。三级都记录下来，相邻两级一比，就知道速度在哪一级变小了。", ""]
        if (self._dir() / "img" / "链路.png").exists():
            out += ["![指令 → 内部 → 实际](img/链路.png)", ""]
        out += ["| 数据 | 是什么 | 从哪来 |", "|-|-|-|",
                "| 指令 | 遥操按摇杆推的位置算出的目标速度，推到底是最大值 | `/cmd_vel` |",
                "| LBMotion 内部 | 行走控制器 LBMotion 限速以后真正要走的速度 | `/lbmotion_states` |",
                "| 实际 | 机器人骨盆真实的移动速度或转动角速度 | 腿部关节角 + IMU |", "",
                "| 对比 | 看出什么 |", "|-|-|",
                "| 内部/指令 | LBMotion 限速了多少。低于 100% 是控制器设定的速度上限，上限数值看“LBMotion 内部”列 |",
                "| 实际/内部 | 机器人的腿能不能走出 LBMotion 要的速度。离 100% 越远，行走执行越差 |",
                "| 实际/指令 | 操作员推杆以后机器人实际给出的比例，等于上面两级相乘 |",
                "| 侧向偏移、朝向变化 | 直着推杆时机器人往侧面偏了多少、车头转了多少 |",
                "| 松杆到停下、多走 | 松开摇杆后多久停住，停住前又走了多远 |", "",
                "单位：前后、横移是 m/s，转向是 °/s。正负：前进、左移、左转为正。汇总表的数值是中位数（最小–最大，样本数）。"
                "项名前的时间是这次推杆开始的时刻，精确到秒。", "",
                "## 链路逐级", "",
               "| 测试项 | 次数 | 指令 | LBMotion 内部 | 实际 | 内部/指令 | 实际/内部 | 实际/指令 |",
               "|-|-|-|-|-|-|-|-|"]
        for r in rows:
            u = " " + r["unit"]
            out.append(f"| {r['kind']} | {r['n']} | {f(r['cmd'], u)} | {f(r['lbm'], u)} | {f(r['actual'], u)} | "
                       f"{f(r['lbm_over_cmd'], pct=True)} | {f(r['actual_over_lbm'], pct=True)} | {f(r['actual_over_cmd'], pct=True)} |")
        out += ["", "## 推杆过程", "", "推杆行程：推杆期间骨盆走的距离。偏移率 = 侧向偏移 ÷ 推杆行程。", "",
                "| 测试项 | 推杆行程 | 侧向偏移（左正） | 偏移率 | 朝向变化（左正） |", "|-|-|-|-|-|"]
        for r in rows:
            out.append(f"| {r['kind']} | {f(r['travel_m'], ' m')} | {f(r['side_m'], ' m')} | {f(r['side_rate'], pct=True)} | "
                       f"{f(r['yaw_push_deg'], '°', n=1)} |")
        out += ["", "## 松杆后", "", "多走：松杆到停下之间走的距离，转向为多转的角度。抬脚次数：松杆后又迈了几步。未停次数：录包结束时还没停下的次数。", "",
                "| 测试项 | 松杆到停下 | 多走（转向为多转） | 朝向变化 | 抬脚次数 | 未停次数 |", "|-|-|-|-|-|-|"]
        for r in rows:
            u = "°" if r["unit"] == "°/s" else " m"
            out.append(f"| {r['kind']} | {f(r['stop_s'], ' s')} | {f(r['coast_main'], u)} | {f(r['coast_yaw_deg'], '°', n=1)} | "
                       f"{f(r['coast_steps'], n=0)} | {r['n_not_stopped']} |")
        out += ["", "## 逐次", "", "| # | 测试项 | 推杆 s | 指令 | LBMotion 内部 | 实际 | 实际/指令 | 侧向偏移 m | 朝向变化 ° | 停下 s | 多走 | 录包 |",
                "|-|-|-|-|-|-|-|-|-|-|-|-|"]
        for t in st["trials"]:
            if t.get("aborted") and t.get("excluded"):
                continue  # 中途停止、选了不保存：不进报告，记录留在 trials.json
            m = t.get("main") or {}
            g = lambda k: "—" if m.get(k) is None else m[k]
            ratio = "—" if m.get("actual_over_cmd") is None else f"{m['actual_over_cmd'] * 100:.1f}%"
            item = f"{ch.hms(t['started'])} {t['item']}"
            if t.get("excluded"):
                item += "（已排除：" + (t.get("exclude_note") or "未写原因") + "）"
            elif t.get("ask"):
                item += "（中途停止，待确认是否计入）"
            elif t.get("aborted"):
                item += "（中途停止）"
            if t.get("workpack"):
                item += f"（工作包“{t['workpack']}”里的推杆，不计入汇总）"
            elif not t.get("excluded") and ch.no_lbm(t):
                item += "（录包里没有 LBMotion 内部速度，不计入汇总）"
            out.append(f"| {t['id']} | {item} | {g('push_s')} | {g('cmd')} | {g('lbm')} | {g('actual')} | {ratio} | "
                       f"{g('side_m')} | {g('yaw_push_deg')} | {g('stop_s')} | {g('coast_main')} | `{t.get('name') or '—'}` |")
        takes = self.takes()
        wps = [k for k in takes if k.get("hands") is not None]
        if wps:
            out += ["", "## 工作包：灵巧手 指令 → 实际", "",
                    "工作包是真人在 VR部分动捕下干活时录的手柄数据，回放给机器人。推杆列在上面“逐次”表，这里看灵巧手："
                    "遥操发给手的开合（`/brainco2/hand/cmd`）和手指实际位置（`/brainco2/hand/state`）比。"
                    "开合度取食指到小指 4 根的平均；指令越过最大开合的一半算一次开合，实际在 2 s 内也越过一半算跟上，响应 = 两者相隔的时间。", "",
                    "| 工作包 | 开始 | 手 | 指令开合次数 | 跟上次数 | 没跟上的时刻 s | 响应 中位 / 最大 s | 开合度误差 中位 / 最大 ° |",
                    "|-|-|-|-|-|-|-|-|"]
            for k in wps:
                hs = (k["hands"] or {}).get("手") or {}
                for side, v in hs.items():
                    if not v.get("指令开合次数"):
                        out.append(f"| {k['item']} | {ch.hms(k['started'])} | {side} | 0 | — | — | — | — |")
                        continue
                    miss = "、".join(str(x) for x in v["没跟上时刻s"]) or "—"
                    out.append(f"| {k['item']} | {ch.hms(k['started'])} | {side} | {v['指令开合次数']} | {v['实际跟上次数']} | {miss} | "
                               f"{v['响应中位s']} / {v['响应最大s']} | {v['开合度误差中位deg']} / {v['开合度误差最大deg']} |")
                if not hs:
                    out.append(f"| {k['item']} | {ch.hms(k['started'])} | — | 录包里没有灵巧手数据 | — | — | — | — |")
        if takes:
            out += ["", "## 录包", "", "| 录包 | 测试项 | 计划次数 | 找到次数 | 状态 | MD5 |", "|-|-|-|-|-|-|"]
            for k in takes:
                out.append(f"| `{k.get('bag') or k['name']}` | {k['item']} | {k['reps']} | {k.get('n_found', '—')} | {k['status']} | "
                           f"`{k.get('md5') or '—'}` |")
        out += ["", "## 方法", "",
                "- 指令：`/cmd_vel`；LBMotion 内部：`/lbmotion_states` data[1]（前后速度）、data[2]（转向角速度），横移没有对应字段。",
                "- 实际：骨盆位移用腿部正运动学 + IMU 姿态、支撑脚不动积分；朝向用骨盆 IMU 偏航。",
                "- 稳态区间：推杆开始 2 s 后到松杆（推杆不足 4 s 时取后一半）；稳态短于 1 s 的次数不计入速度比。",
                "- 停下：平移看骨盆水平速度连续 0.5 s < 0.05 m/s，转向看偏航角速度连续 0.5 s < 5°/s。",
                "- 抬脚：膝关节比推杆前站立值多屈 0.3 rad 记一次。"]
        return "\n".join(out) + "\n"

    # ---------- 其他 ----------
    @staticmethod
    def _exec(client, cmd: str, timeout: float = 60) -> str:
        _, o, e = client.exec_command(cmd, timeout=timeout)
        out = o.read().decode(errors="replace")
        code = o.channel.recv_exit_status()
        if code:
            raise RuntimeError(f"主控命令失败（{code}）：{(out + e.read().decode(errors='replace')).strip()[-300:]}")
        return out

    def analyze_local(self, path: str) -> dict:
        p = Path(path).expanduser()
        if not p.is_file() or p.suffix != ".bag":
            raise ValueError("填本机上的 .bag 文件路径")
        segs = [s for s in an.analyze_bag(str(p)) if not s["cut_start"]]
        take = self._append("takes.json", [{"item": "已有录包", "reps": 0, "name": p.stem, "started": p.stat().st_mtime,
                                            "ended": p.stat().st_mtime, "bag": str(p), "md5": "", "size": p.stat().st_size,
                                            "status": "完成" if segs else "没有有效推杆", "n_found": len(segs), "n_other": 0,
                                            "note": "本机已有录包"}])[0]
        self._append("trials.json", [{"take": take["id"], "item": s["kind"], "name": p.stem, "started": p.stat().st_mtime + s["t"],
                                      "bag": str(p), "md5": "", "status": "完成", "main": s, "segments": []} for s in segs])
        self.write_report()
        return take

    def fetch_bags(self) -> None:
        if self.fetching:
            raise ValueError("正在拷")
        todo = [k for k in self.takes() if k.get("bag", "").startswith(REMOTE) and not k.get("local_bag")]
        if not todo:
            raise ValueError("没有需要拷回的录包")
        self.fetching = f"0/{len(todo)}"
        threading.Thread(target=self._fetch, args=(todo,), daemon=True).start()

    def _fetch(self, todo: list[dict]) -> None:
        out = self._dir() / "bag"
        out.mkdir(exist_ok=True)
        try:
            c = self._ssh_factory()
            home = self._exec(c, "echo $HOME").strip()
            sftp = c.open_sftp()
            for i, k in enumerate(todo, 1):
                dst = out / f"{k['name']}.bag"
                sftp.get(k["bag"].replace("~", home, 1), str(dst))
                h = hashlib.md5()
                with dst.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                ok = h.hexdigest() == k["md5"]
                self._update("takes.json", k["id"], local_bag=str(dst) if ok else "",
                             note=(k.get("note") or "") + ("" if ok else "；拷回 MD5 不一致"))
                self.fetching = f"{i}/{len(todo)}"
            sftp.close()
            c.close()
        except Exception as exc:  # noqa: BLE001
            self.error = f"拷回录包失败：{exc}"
        finally:
            self.fetching = ""

    def open_folder(self) -> str:
        d = self._dir()
        subprocess.Popen(["xdg-open", str(d)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return str(d)
