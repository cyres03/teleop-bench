"""全身遥操（WBT）回放测试：给第一次用的人设计，选动作 → 点“开始” → 等它播完出结果。

原理：人的动作事先用 Pico 全身动捕录好（assets/wbt/*.bag，只含 /vr_cmd 和人体 /tf），回放给机器人，
机器人当成真人在遥操。同时录下重定向输出和机器人实际，比较 人 → 重定向 → 机器人 每一级差多少。
输入每次都一样，所以不同版本、不同机器人之间可以直接比。

流程：
1. 准备（换机器人时自动做）：SSH → 同步回放工具和回放包（按 MD5）→ mrosrecord/mrosplay 在 → 磁盘 →
   没有别的录包或回放 → 遥操节点在跑 → /vr_cmd 没有别的发布者（Pico 必须断开，否则两路指令混发会抖）。
2. 开始：主控后台录包 → 3 s 后回放（pack.py play：时间戳改成现在，再 mrosplay）→ 播完 2 s 停止录包。
3. 拷回本机、核 MD5 → 分析 → 出图 → 更新本场报告（~/.teleop-bench/wbt/<场次>/报告.md）。
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..db import data_dir
from ..lbm.runner import pose_cmd
from ..robotstream import MROS
from . import analyze as an
from . import report as rp

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parents[2] / "assets" / "wbt"
DEMO_BAG = ASSETS / "demo" / "右行_样本.bag"
TOOLS = HERE.parents[1] / "tools"
REMOTE = "~/Cyres/wbt"
TOOL_FILES = {"pack.py": HERE / "pack.py", "bag_extract.py": HERE.parent / "lbm" / "bag_extract.py",
              "rosbag_lite.py": HERE.parent / "lbm" / "rosbag_lite.py"}
RECORD = ("tf", "teleop/wb_cmd", "teleop/state", "teleop/session_state", "ImuData", "joint/state", "vr_cmd", "cmd_vel",
          "brainco2/hand/cmd", "brainco2/hand/state")
MIN_FREE_GB = 3
HZ_S = 12        # mrostopic hz 要等多久：mros 话题握手 7 s 以上才出第一个数
LEAD_S = 3.0      # 开始录包后多久开始回放（mros 话题握手）
AFTER_S = 2.0     # 播完再录多久
BASELINE = "在 660 上用 Pico + 5 个 tracker 录的全身动捕：前 6 个是 11-19 录的基线动作，后 2 个是 11-20 录的灵巧手工作包"
ITEMS = [
    {"key": "前进", "what": "前进 5 m", "space": "机器人前方留出 7 m 空地"},
    {"key": "后退", "what": "后退 5 m", "space": "机器人后方留出 7 m 空地"},
    {"key": "左行", "what": "横移 3 m", "space": "机器人左右两侧各留出 4 m 空地"},
    {"key": "右行", "what": "横移 3 m", "space": "机器人左右两侧各留出 4 m 空地"},
    {"key": "SKU", "what": "原地 SKU 展示动作 40 s（抬手、弯腰、转身）", "space": "机器人周围 2 m 内没有人和东西"},
    {"key": "转身", "what": "原地右转 2 圈，再左转 2 圈", "space": "机器人周围 2 m 内没有人和东西"},
    {"key": "灵巧手捡瓶_前40s", "what": "工作包前 40 s：深蹲、弯腰，手伸到地面",
     "space": "机器人周围 1.5 m 内没有人，天车绳长够骨盆往下 0.4 m（会深蹲到手贴地）"},
    {"key": "灵巧手捡瓶", "what": "工作包 170 s：深蹲弯腰够地上的瓶子，再站着取放",
     "space": "机器人周围 1.5 m 内没有人，天车绳长够骨盆往下 0.4 m（会深蹲到手贴地、侧身弯腰）"},
]
ITEM = {i["key"]: i for i in ITEMS}
# 版本对比看的主方向：(分级, 字段)
MAIN = {"前进": ("位移", "前进"), "后退": ("位移", "前进"), "左行": ("位移", "侧移"), "右行": ("位移", "侧移"),
        "转身": ("右转", None), "SKU": ("偏航", None)}


def steps(item: str) -> list[str]:
    i = ITEM[item]
    out = [
        f"机器人挂好天车，{i['space']}。天车要有人跟着推。",
        "遥控器拿在手上，随时准备急停。",
        "关掉 Pico 或退出遥操 App。不用人遥操，机器人的动作全部来自录好的包。",
        "机器人在 Walk 状态站稳（遥控器或 Pico 按 RG+B）。",
    ]
    if "灵巧手" in item:
        out.append("场地空着就行，不用摆物体：只看机器人和灵巧手的动作。")
    return out + [
        f"点“开始”。软件播放“{i['key']}”，3 秒后机器人自己进入全身遥操，做：{i['what']}。",
        "播完机器人自动回到 Walk。中途有危险：先按遥控器急停，再点“停止”。",
    ]


def brief(item: str, res: dict) -> dict:
    """版本对比用的几项：主方向 重定向 → 机器人 和完成率、偏航净差、关节误差均方根中位和最大、差距条数。"""
    s1, s2 = res["人→重定向"], res["重定向→机器人"]
    kind, key = MAIN.get(item, ("偏航", None))
    if kind == "位移" and s2.get("位移"):
        r, m, unit = s1["位移"]["重定向"][key], s2["位移"]["机器人"][key], "m"
    elif kind == "右转" and s2.get("偏航", {}).get("机器人转动"):
        r, m, unit = abs(s2["偏航"]["重定向转动"]["先转"]), abs(s2["偏航"]["机器人转动"]["先转"]), "°"
    elif s2.get("偏航"):
        r, m, unit = s2["偏航"]["重定向净"], s2["偏航"]["机器人净"], "°"
    else:
        r = m = None
        unit = ""
    js = sorted(j["均方根deg"] for j in s2.get("关节") or [])
    y = s2.get("偏航") or {}
    return {"主方向": (key or kind) if r is not None else "—", "重定向": r, "机器人": m, "单位": unit,
            "完成率": round(m / r, 2) if r and abs(r) >= 0.5 else None,
            "偏航净差": round(y["机器人净"] - y["重定向净"], 1) if y else None,
            "关节误差中位": js[len(js) // 2] if js else None, "关节误差最大": js[-1] if js else None,
            "差距数": sum(i["环节"] == "重定向→机器人" for i in res["差距清单"])}


def pack_path(item: str) -> Path:
    return ASSETS / f"{item}.bag"


def _md5(p: Path) -> str:
    h = hashlib.md5()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_info: dict[str, dict] = {}


def pack_info(item: str) -> dict:
    """回放包时长、MD5（缓存）。"""
    p = pack_path(item)
    if item not in _info:
        from ..lbm import rosbag_lite as rl
        _, first, last = rl.Bag(str(p)).summary()
        _info[item] = {"seconds": round(max(last.values()) - min(first.values()), 1), "md5": _md5(p), "mb": round(p.stat().st_size / 1e6, 1)}
    return _info[item]


# ---------------- 主控命令 ----------------
def record_cmd(name: str) -> str:
    keep = "|".join(re.escape(t) for t in RECORD)
    n = shlex.quote(name)
    return (f"mkdir -p {REMOTE}/bag && cd {REMOTE}/bag && "
            "if pgrep -x 'mrosrecord|mrosplay' >/dev/null; then echo BUSY; exit 3; fi; "
            f"{MROS}setsid nohup mrosrecord -q -O {n} -a -x {shlex.quote(f'^/(?!({keep})$).*')} > {n}.log 2>&1 < /dev/null & echo PID $!")


def play_cmd(name: str, item: str) -> str:
    n, p = shlex.quote(name), shlex.quote(f"{item}.bag")
    return (f"cd {REMOTE}/tools && {MROS}setsid nohup python3 pack.py play ../packs/{p} "
            f"> ../bag/{n}.play.log 2>&1 < /dev/null & echo PID $!")


RETARGET_CFG = "picoTF"


def retarget_cmd(action: str) -> str:
    """/retarget/status 查状态；start 启动 picoTF 重定向（真人用 Pico 时由 Pico 那边调，回放要自己调）；stop 恢复。"""
    srv = {"status": ("/retarget/status", "std_srvs/GetString", "{}"),
           "start": ("/retarget/start", "std_srvs/SetString", json.dumps({"data": RETARGET_CFG})),
           "stop": ("/retarget/stop", "std_srvs/Trigger", "{}")}[action]
    return f"{MROS}timeout 15 mrosservice call {srv[0]} {srv[1]} {shlex.quote(srv[2])} 2>&1 | grep -m1 response || true"


def parse_response(out: str) -> dict:
    m = re.search(r"response:\s*(\{.*\})", out)
    return json.loads(m.group(1)) if m else {}


def stop_cmd(name: str, pid: int) -> str:
    n = shlex.quote(name)
    return (f"cd {REMOTE}/bag; kill -INT -- -{int(pid)} 2>/dev/null; "
            f"for i in $(seq 1 60); do [ -f {n}.bag ] && [ ! -f {n}.bag.active ] && break; sleep 0.5; done; "
            f"[ -f {n}.bag ] || {{ echo NOBAG; exit 4; }}; md5sum {n}.bag | tee {n}.bag.md5; stat -c %s {n}.bag")


class WbtController:
    def __init__(self, ssh_factory=None):
        self._ssh_factory = ssh_factory
        self.robot = self.version = self.battery = ""
        self.simulated = False
        self.lock = threading.Lock()
        self.prep: dict = {"robot": "", "running": False, "checks": [], "ok": False}
        self.run: dict | None = None      # 当前这一次：阶段、进度
        self._stop = threading.Event()
        self.error = ""

    # ---------- 会话和存储 ----------
    def bind(self, *, robot: str, version: str, simulated: bool, battery: str = "") -> None:
        self.robot, self.version, self.simulated, self.battery = robot, version, simulated, battery
        busy = self.run and self.run.get("active")
        if not busy and self.prep.get("robot") != (robot, simulated):
            self.prepare()

    @property
    def session(self) -> str:
        return ("演练 " if self.simulated else "") + time.strftime("%Y-%m-%d") + " " + (self.robot or "未识别")

    def _dir(self) -> Path:
        d = data_dir() / "wbt" / self.session.replace(" ", "_")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def runs(self) -> list[dict]:
        f = self._dir() / "runs.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []

    def _save_run(self, row: dict) -> dict:
        with self.lock:
            rows = self.runs()
            row["id"] = max([r["id"] for r in rows], default=0) + 1
            rows.append(row)
            (self._dir() / "runs.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        return row

    def delete(self, rid: int) -> bool:
        with self.lock:
            rows = self.runs()
            keep = [r for r in rows if r["id"] != rid]
            (self._dir() / "runs.json").write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
        self.write_report()
        return len(keep) != len(rows)

    def keep(self, rid: int, keep: bool) -> None:
        """中途停止的那次要不要计入报告。不计入只是不进报告，录包和记录都留着。"""
        with self.lock:
            rows = self.runs()
            for r in rows:
                if r["id"] == rid:
                    r.update(ask=False, excluded=not keep)
            (self._dir() / "runs.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        self.write_report()

    def history(self) -> list[dict]:
        """所有真机场次里播当前基线包的结果（回放包 MD5 对得上、没中途停止），每个（动作，版本，机器人）取最新一次。"""
        latest: dict[tuple, dict] = {}
        for f in sorted((data_dir() / "wbt").glob("*/runs.json")):
            if f.parent.name.startswith("演练"):
                continue
            for r in json.loads(f.read_text(encoding="utf-8")):
                if r.get("aborted") or r["item"] not in ITEM or r.get("pack_md5") != pack_info(r["item"])["md5"]:
                    continue
                row = {"item": r["item"], "version": r.get("version") or "—", "robot": r.get("robot") or "—",
                       "date": f.parent.name.split("_")[0], "time": r["time"], **brief(r["item"], r["result"])}
                latest[(r["item"], row["version"], row["robot"])] = row
        order = {i["key"]: k for k, i in enumerate(ITEMS)}
        return sorted(latest.values(), key=lambda x: (order[x["item"]], x["date"], x["time"]))

    def image(self, rid: int) -> Path | None:
        p = self._dir() / "img" / f"{rid}.png"
        return p if p.exists() else None

    def state(self) -> dict:
        return {
            "session": self.session, "robot": self.robot, "version": self.version, "simulated": self.simulated,
            "items": [dict(i, steps=steps(i["key"]), **pack_info(i["key"])) for i in ITEMS],
            "prep": self.prep, "run": self.run, "error": self.error, "runs": self.runs(),
            "baseline": BASELINE, "history": self.history(),
            "report_path": str(self._dir() / "报告.md") if (self._dir() / "报告.md").exists() else "",
            "thresholds": {k: list(v) if isinstance(v, tuple) else v for k, v in an.TH.items()},
        }

    # ---------- 1. 准备 ----------
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
                for n in ("连接机器人", "回放工具和动作包", "录包工具", "磁盘空间", "遥操节点", "重定向模块", "Pico 已断开"):
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
                self._check("连接机器人", True, f"{self.robot}，{self.version or '版本未读到'}" + (f"，电量 {self.battery}" if self.battery else ""))
                self._check("回放工具和动作包", None, "核对…")
                n = self._sync(c)
                self._check("回放工具和动作包", True, f"已上传 {n} 个文件" if n else "已是最新")
                out = self._exec(c, f"{MROS}which mrosrecord mrosplay python3 || true; df -BG --output=avail ~ | tail -1; "
                                    "pgrep -x 'mrosrecord|mrosplay' || true")
                lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
                has = sum(ln.endswith(("/mrosrecord", "/mrosplay", "/python3")) for ln in lines) == 3
                self._check("录包工具", has, "mrosrecord、mrosplay 可用" if has else "主控上缺 mrosrecord / mrosplay / python3")
                gb = next((int(ln.rstrip("G")) for ln in lines if re.fullmatch(r"\d+G", ln)), 0)
                self._check("磁盘空间", gb >= MIN_FREE_GB, f"剩余 {gb} GB")
                busy = [ln for ln in lines if ln.isdigit()]
                if busy:
                    self._check("没有别的录包", False, f"主控上已有录包或回放在跑（pid {' '.join(busy)}），先停掉")
                self._check("遥操节点", None, "看 /teleop/state…")
                tel = self._hz(c, "/teleop/state")
                self._check("遥操节点", tel > 0, f"/teleop/state {tel:.0f} Hz" if tel > 0 else "/teleop/state 没有数据：遥操节点没在跑")
                st = self._retarget(c, "status").get("data", "")
                self._check("重定向模块", bool(st), f"现在是 {st}，点开始时自动启动 {RETARGET_CFG}，测完恢复" if st == "idle"
                            else (f"现在是 {st}" if st else "/retarget/status 没响应：遥操节点版本不对？"))
                pico = self._pico_check(c)
                p["ok"] = has and gb >= MIN_FREE_GB and not busy and tel > 0 and bool(st) and pico
            finally:
                c.close()
        except Exception as exc:  # noqa: BLE001
            self._check("准备", False, str(exc))
        finally:
            p["running"] = False

    def _pico_check(self, c) -> bool:
        self._check("Pico 已断开", None, "看 /vr_cmd 有没有别人在发…")
        hz = self._hz(c, "/vr_cmd")
        ok = hz == 0
        self._check("Pico 已断开", ok, "/vr_cmd 没有发布者" if ok else f"/vr_cmd 还在发（{hz:.0f} Hz）：退出 Pico 遥操 App 或关掉 Pico")
        return ok

    def _retarget(self, c, action: str) -> dict:
        return parse_response(self._exec(c, retarget_cmd(action), timeout=30))

    def _hz(self, c, topic: str) -> float:
        out = self._exec(c, f"{MROS}timeout {HZ_S} mrostopic hz {topic} 2>&1 | grep -m1 'average rate' || true", timeout=HZ_S + 15)
        m = re.search(r"average rate:\s*([\d.]+)", out)
        return float(m.group(1)) if m else 0.0

    def _sync(self, c) -> int:
        files = {f"tools/{k}": v for k, v in TOOL_FILES.items()} | {f"packs/{i['key']}.bag": pack_path(i["key"]) for i in ITEMS}
        want = {k: _md5(v) for k, v in files.items()}
        out = self._exec(c, f"mkdir -p {REMOTE}/tools {REMOTE}/packs {REMOTE}/bag && cd {REMOTE} && "
                            f"md5sum {' '.join(shlex.quote(k) for k in files)} 2>/dev/null || true")
        have = {ln.split()[1]: ln.split()[0] for ln in out.splitlines() if len(ln.split()) == 2}
        todo = [k for k in files if have.get(k) != want[k]]
        if todo:
            home = self._exec(c, "echo $HOME").strip()
            sftp = c.open_sftp()
            try:
                for k in todo:
                    sftp.put(str(files[k]), f"{home}/Cyres/wbt/{k}")
            finally:
                sftp.close()
        return len(todo)

    # ---------- 2. 回放 ----------
    def begin(self, item: str) -> None:
        if item not in ITEM:
            raise ValueError("没有这个动作")
        if self.run and self.run.get("active"):
            raise ValueError("上一次还没结束")
        if not self.prep.get("ok"):
            raise ValueError("机器人还没准备好：看上面的检查清单")
        name = f"{time.strftime('%m%d_%H%M%S')}_{item}"
        self._stop.clear()
        self.error = ""
        self.run = {"active": True, "item": item, "name": name, "stage": "检查", "text": "确认 Pico 已断开…",
                    "elapsed": 0.0, "total": pack_info(item)["seconds"], "started": time.time()}
        threading.Thread(target=self._run_sim if self.simulated else self._run_robot, args=(item, name), daemon=True).start()

    def stop(self) -> None:
        if not (self.run and self.run.get("active")):
            raise ValueError("没有在回放")
        self._stop.set()

    def _stage(self, stage: str, text: str = "") -> None:
        self.run.update(stage=stage, text=text)

    def _run_robot(self, item: str, name: str) -> None:
        r = self.run
        c = rec_pid = old_mode = None
        aborted = started_rt = False
        try:
            c = self._ssh_factory()
            if not self._pico_check(c):
                raise RuntimeError("Pico 还连着：/vr_cmd 有别的发布者。退出 Pico 遥操 App 后再点开始")
            # 预选模式必须是 VR全身动捕（2），否则 RG+A 会进 VR部分动捕；测完恢复原值
            self._stage("检查", "确认遥操预选模式是 VR全身动捕…")
            old_mode = parse_response(self._exec(c, pose_cmd())).get("data")
            if old_mode is None:
                raise RuntimeError("读不到遥操预选模式（/teleop/get_pose_ctrl_mode 没响应）：遥操节点在跑吗")
            if old_mode != 2:
                self._exec(c, pose_cmd(2))
                if parse_response(self._exec(c, pose_cmd())).get("data") != 2:
                    raise RuntimeError("预选模式没切成 VR全身动捕")
            self._stage("检查", "启动重定向（picoTF）…")
            if self._retarget(c, "status").get("data", "idle") == "idle":
                rsp = self._retarget(c, "start")
                st = self._retarget(c, "status").get("data", "")
                if not rsp.get("success") or st in ("", "idle"):
                    raise RuntimeError(f"重定向启动失败：{rsp.get('message') or st or '服务没响应'}")
                started_rt = True
            self._stage("录包", "开始录包…")
            out = self._exec(c, record_cmd(name), timeout=20)
            if "BUSY" in out:
                raise RuntimeError("主控上已有录包或回放在跑，先停掉")
            rec_pid = int(re.search(r"PID (\d+)", out).group(1))
            self._wait(LEAD_S)
            if self._stop.is_set():
                raise RuntimeError("已停止，没有开始回放")
            self._stage("回放", "机器人正在照录好的动作做，盯着它，手放在急停上")
            out = self._exec(c, play_cmd(name, item), timeout=20)
            play_pid = int(re.search(r"PID (\d+)", out).group(1))
            t0 = time.time()
            while True:
                alive = self._exec(c, f"kill -0 {play_pid} 2>/dev/null && echo RUN || echo END").strip() == "RUN"
                r["elapsed"] = round(time.time() - t0, 1)
                if not alive:
                    break
                if self._stop.is_set() or r["elapsed"] > r["total"] + 30:
                    self._exec(c, f"kill -INT -- -{play_pid} 2>/dev/null; kill -INT {play_pid} 2>/dev/null; true")
                    aborted = True
                    break
                time.sleep(0.5)
            # mrosplay 播完退出时会段错误，不影响回放；看有没有开始播（PLAY）和播完（Done.）
            log = self._exec(c, f"cat {REMOTE}/bag/{shlex.quote(name)}.play.log 2>/dev/null || true")
            if "PLAY" not in log or ("Done." not in log and not aborted):
                raise RuntimeError(f"回放没播完：{log.strip()[-300:] or '没有日志'}")
            self._stage("停止录包", "播完了，再录 2 秒…")
            self._wait(AFTER_S)
            out = self._exec(c, stop_cmd(name, rec_pid), timeout=60)
            rec_pid = None
            md5 = out.split()[0]
            self._stage("拷回", "把录包拷回本机…")
            local = self._dir() / "bag" / f"{name}.bag"
            local.parent.mkdir(exist_ok=True)
            home = self._exec(c, "echo $HOME").strip()
            sftp = c.open_sftp()
            try:
                sftp.get(f"{home}/Cyres/wbt/bag/{name}.bag", str(local))
            finally:
                sftp.close()
            if _md5(local) != md5:
                raise RuntimeError("拷回的录包 MD5 对不上，重新点开始")
            self._finish(item, name, local, md5, aborted=aborted)
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            if c and rec_pid:
                try:
                    self._exec(c, stop_cmd(name, rec_pid), timeout=60)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            if c and old_mode is not None and old_mode != 2:
                try:
                    self._exec(c, pose_cmd(old_mode))
                except Exception:  # noqa: BLE001
                    pass
            if c and started_rt:
                try:
                    self._retarget(c, "stop")
                except Exception:  # noqa: BLE001
                    pass
            if c:
                c.close()
            r["active"] = False

    def _run_sim(self, item: str, name: str) -> None:
        """演练：不连机器人，按真实节奏走一遍，结果用自带的右行样本包。"""
        r = self.run
        try:
            self._stage("录包", "演练：开始录包…")
            self._wait(1.0)
            self._stage("回放", "演练：机器人正在照录好的动作做")
            t0 = time.time()
            total = min(r["total"], 8.0)
            while time.time() - t0 < total and not self._stop.is_set():
                r["elapsed"] = round((time.time() - t0) / total * r["total"], 1)
                time.sleep(0.2)
            self._stage("拷回", "演练：拷回…")
            local = self._dir() / "bag" / f"{name}.bag"
            local.parent.mkdir(exist_ok=True)
            shutil.copyfile(DEMO_BAG, local)
            self._finish(item, name, local, _md5(local), aborted=self._stop.is_set(), note="演练：结果是自带的右行样本包")
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
        finally:
            r["active"] = False

    def _wait(self, s: float) -> None:
        self._stop.wait(s)

    # ---------- 3. 分析和报告 ----------
    def _finish(self, item: str, name: str, bag: Path, md5: str, aborted: bool = False, note: str = "") -> dict:
        if self.run:
            self._stage("分析", "算 人 → 重定向 → 机器人 的差距…")
        res = an.analyze_bag(str(bag))
        img = self._dir() / "img"
        img.mkdir(exist_ok=True)
        # 先把图完整画好再登记这一次：界面一看到新记录就取图，取到半张会一直缓存着
        tmp = img / f"{name}.tmp.png"
        rp.chart(res, tmp, f"{item}（{name}）")
        with self.lock:
            rid = max([r["id"] for r in self.runs()], default=0) + 1
            tmp.replace(img / f"{rid}.png")
        row = self._save_run({"item": item, "name": name, "time": time.strftime("%H:%M:%S"), "bag": str(bag), "md5": md5,
                              "pack_md5": pack_info(item)["md5"] if item in ITEM else "", "aborted": aborted, "ask": aborted, "note": note,
                              "robot": self.robot, "version": self.version, "result": an.public(res)})
        assert row["id"] == rid
        self.write_report()
        if self.run:
            self._stage("完成", "出结果了，看下面")
        return row

    def analyze_local(self, path: str, item: str = "") -> dict:
        p = Path(path).expanduser()
        if not p.is_file() or p.suffix != ".bag":
            raise ValueError("填本机上的 .bag 文件路径")
        guess = item or ("转身" if "转" in p.stem else next((i["key"] for i in ITEMS if i["key"] in p.stem), "已有录包"))
        return self._finish(guess, p.stem, p, _md5(p), note="离线分析已有录包")

    def write_report(self) -> tuple[str, dict]:
        p = self._dir() / "报告.md"
        runs = [r for r in self.runs() if not r.get("excluded")]
        jm = self._dir() / "img" / "关节.png"
        jm.parent.mkdir(exist_ok=True)
        if not rp.joint_map(runs, jm):
            jm.unlink(missing_ok=True)
        p.write_text(rp.markdown(self.session, self.robot, self.version, runs, self._dir(), self.history()), encoding="utf-8")
        r = subprocess.run([sys.executable, str(TOOLS / "lint_report_style.py"), "--file", str(p)],
                           capture_output=True, text=True, timeout=60)
        m = re.search(r"错误 (\d+)，警告 (\d+)", r.stdout)
        return str(p), {"errors": int(m.group(1)) if m else -1, "warnings": int(m.group(2)) if m else -1}

    def open_folder(self) -> str:
        d = self._dir()
        subprocess.Popen(["xdg-open", str(d)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return str(d)

    @staticmethod
    def _exec(client, cmd: str, timeout: float = 60) -> str:
        _, o, e = client.exec_command(cmd, timeout=timeout)
        out = o.read().decode(errors="replace")
        code = o.channel.recv_exit_status()
        if code:
            raise RuntimeError(f"主控命令失败（{code}）：{(out + e.read().decode(errors='replace')).strip()[-300:]}")
        return out
