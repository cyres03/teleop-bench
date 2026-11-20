"""本地 SQLite 台账。数据目录默认 ~/.teleop-bench，可用环境变量 TELEOP_BENCH_HOME 改。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from .metrics import (LAYERS, METRICS, f1_veto, higher_regressed, indicator, lower_regressed, meets_target,
                      status_of)

_LOCK = threading.Lock()


def data_dir() -> Path:
    path = Path(os.environ.get("TELEOP_BENCH_HOME", Path.home() / ".teleop-bench"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(data_dir() / "bench.db")
    conn.row_factory = sqlite3.Row
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    product TEXT NOT NULL DEFAULT 'Oli',
    scene TEXT NOT NULL,
    issue_type TEXT NOT NULL,
    metric TEXT NOT NULL,
    total REAL NOT NULL,
    events INTEGER NOT NULL,
    target REAL,
    version TEXT NOT NULL DEFAULT '',
    robot TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '手动',
    note TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    outcomes TEXT NOT NULL DEFAULT '',
    layer TEXT NOT NULL DEFAULT '',
    flat INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS latency_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    link TEXT NOT NULL DEFAULT '',
    robot TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    calib_ms REAL,
    result TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS measures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    scene TEXT NOT NULL,
    metric TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    value REAL NOT NULL,
    max REAL,
    n INTEGER NOT NULL DEFAULT 0,
    target REAL,
    layer TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    robot TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS boundary_trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    session TEXT NOT NULL,
    action TEXT NOT NULL,
    result TEXT NOT NULL,
    boundary TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    sway TEXT NOT NULL DEFAULT '',
    controller TEXT NOT NULL DEFAULT '',
    robot TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT ''
);
"""

# 旧库升级：只加列，不改已有数据
MIGRATIONS = (
    ("records", "layer", "TEXT NOT NULL DEFAULT ''"),
    ("records", "flat", "INTEGER NOT NULL DEFAULT 1"),
)


def init() -> None:
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)
        for table, col, decl in MIGRATIONS:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _row_to_record(row: sqlite3.Row) -> dict:
    rec = dict(row)
    ind = indicator(rec["metric"], rec["total"], rec["events"])
    rec["display"] = ind.display
    rec["ratio"] = ind.ratio
    rec["small_sample"] = ind.small_sample
    rec["meets_target"] = meets_target(ind, rec["target"])
    rec["outcomes"] = rec["outcomes"] or ""
    rec["f1"] = f1_veto(rec["metric"], rec["events"], rec["flat"])
    return rec


def add_record(**fields) -> dict:
    required = ("scene", "issue_type", "metric", "total", "events")
    missing = [k for k in required if fields.get(k) in (None, "")]
    if missing:
        raise ValueError(f"缺少字段：{', '.join(missing)}")
    if fields["metric"] not in METRICS:
        raise ValueError(f"指标只能是 {' / '.join(METRICS)}")
    if fields.get("layer") and fields["layer"] not in LAYERS:
        raise ValueError(f"层级只能是 {' / '.join(LAYERS)}")
    if "flat" in fields and fields["flat"] is not None:
        fields["flat"] = 1 if fields["flat"] in (True, 1, "1", "true") else 0
    indicator(fields["metric"], float(fields["total"]), int(fields["events"]))  # 校验
    cols = ("product", "scene", "issue_type", "metric", "total", "events", "target",
            "version", "robot", "source", "note", "evidence", "outcomes", "layer", "flat")
    values = {k: fields.get(k) for k in cols if fields.get(k) is not None}
    values["created_at"] = fields.get("created_at") or time.time()
    keys = list(values)
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            f"INSERT INTO records ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
            [values[k] for k in keys],
        )
        row = conn.execute("SELECT * FROM records WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_record(row)


def list_records() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute("SELECT * FROM records ORDER BY scene, issue_type, created_at DESC").fetchall()
    return annotate_trend([_row_to_record(r) for r in rows])


def _version_order(rows: list[dict]) -> list[str]:
    first: dict[str, float] = {}
    for r in rows:
        if r["version"]:
            first[r["version"]] = min(first.get(r["version"], r["created_at"]), r["created_at"])
    return sorted(first, key=first.get)


def annotate_trend(recs: list[dict]) -> list[dict]:
    """同一（场景，问题类型，指标）按固件版本合计；第一版是基线，和上一版比是否回退，再给出判定。"""
    groups: dict[tuple, list[dict]] = {}
    for r in recs:
        groups.setdefault((r["scene"], r["issue_type"], r["metric"]), []).append(r)
    for (_, _, metric), rows in groups.items():
        order = _version_order(rows)
        per = {}
        for v in order:
            mine = [r for r in rows if r["version"] == v]
            per[v] = indicator(metric, sum(r["total"] for r in mine), sum(r["events"] for r in mine))
        for r in rows:
            v = r["version"]
            r["baseline_version"] = order[0] if order else ""
            r["is_baseline"] = bool(v) and v == r["baseline_version"]
            idx = order.index(v) if v in order else -1
            r["version_display"] = per[v].display if v in per else ""
            regressed = False
            if idx > 0:
                prev = order[idx - 1]
                r["prev_version"], r["prev_display"] = prev, per[prev].display
                regressed = higher_regressed(per[prev], per[v])
            else:
                r["prev_version"], r["prev_display"] = "", ""
            r["regressed"] = regressed
            r["status"], r["status_text"] = status_of(veto=r["f1"], meets=r["meets_target"], regressed=regressed,
                                                      is_baseline=r["is_baseline"])
    return recs


def delete_record(record_id: int) -> bool:
    with _LOCK, _connect() as conn:
        return conn.execute("DELETE FROM records WHERE id = ?", (record_id,)).rowcount > 0


def ledger_markdown(records: list[dict]) -> str:
    """按 Phil 的写法导出，可直接贴进飞书：场景，CPI = x（总/问题）。"""
    lines = []
    for rec in records:
        ind = indicator(rec["metric"], rec["total"], rec["events"])
        extra = "，单点参考" if ind.small_sample else ""
        tag = f"，{rec['status_text']}" if rec.get("status") in ("f1", "fail", "regress") else ""
        lines.append(f"- {rec['scene']}（{rec['issue_type']}），{rec['metric']} = {ind.text}{extra}{tag}")
    return "\n".join(lines)


def add_latency_run(kind: str, name: str, result: dict, **fields) -> dict:
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO latency_runs (created_at, kind, name, link, robot, version, calib_ms, result, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), kind, name, fields.get("link", ""), fields.get("robot", ""),
             fields.get("version", ""), fields.get("calib_ms"), json.dumps(result, ensure_ascii=False),
             fields.get("note", "")),
        )
        row = conn.execute("SELECT * FROM latency_runs WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _latency_row(row)


def _latency_row(row: sqlite3.Row) -> dict:
    rec = dict(row)
    rec["result"] = json.loads(rec["result"])
    return rec


def list_latency_runs() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute("SELECT * FROM latency_runs ORDER BY created_at DESC").fetchall()
    return [_latency_row(r) for r in rows]


def delete_latency_run(run_id: int) -> bool:
    with _LOCK, _connect() as conn:
        return conn.execute("DELETE FROM latency_runs WHERE id = ?", (run_id,)).rowcount > 0


# ---------- 测量值（摆幅、Ts 等越小越好的量） ----------
def add_measure(**f) -> dict:
    for k in ("scene", "metric", "value"):
        if f.get(k) in (None, ""):
            raise ValueError(f"缺少字段：{k}")
    cols = ("scene", "metric", "unit", "value", "max", "n", "target", "layer", "version", "robot", "source", "note")
    vals = {k: f[k] for k in cols if f.get(k) is not None}
    vals["created_at"] = f.get("created_at") or time.time()
    keys = list(vals)
    with _LOCK, _connect() as conn:
        cur = conn.execute(f"INSERT INTO measures ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                           [vals[k] for k in keys])
        row = conn.execute("SELECT * FROM measures WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def list_measures() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM measures ORDER BY scene, metric, created_at DESC")]
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["scene"], r["metric"]), []).append(r)
    for rs in groups.values():
        order = _version_order(rs)
        per = {v: sum(x["value"] for x in rs if x["version"] == v) / sum(1 for x in rs if x["version"] == v)
               for v in order}
        for r in rs:
            v = r["version"]
            idx = order.index(v) if v in order else -1
            r["is_baseline"] = idx == 0
            r["prev_version"] = order[idx - 1] if idx > 0 else ""
            r["prev_value"] = round(per[order[idx - 1]], 2) if idx > 0 else None
            regressed = idx > 0 and lower_regressed(per[order[idx - 1]], per[v])
            meets = None if r["target"] is None else r["value"] <= r["target"]
            r["regressed"] = regressed
            r["status"], r["status_text"] = status_of(veto=False, meets=meets, regressed=regressed,
                                                      is_baseline=r["is_baseline"])
    return rows


def delete_measure(mid: int) -> bool:
    with _LOCK, _connect() as conn:
        return conn.execute("DELETE FROM measures WHERE id = ?", (mid,)).rowcount > 0


# ---------- 边界探测逐次记录 ----------
def add_trial(**f) -> dict:
    cols = ("session", "action", "result", "boundary", "note", "sway", "controller", "robot", "version")
    vals = {k: f.get(k, "") or "" for k in cols}
    vals["created_at"] = time.time()
    keys = list(vals)
    with _LOCK, _connect() as conn:
        cur = conn.execute(f"INSERT INTO boundary_trials ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                           [vals[k] for k in keys])
        row = conn.execute("SELECT * FROM boundary_trials WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def list_trials(session: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM boundary_trials WHERE session = ? ORDER BY created_at", (session,))]


def delete_trial(tid: int) -> bool:
    with _LOCK, _connect() as conn:
        return conn.execute("DELETE FROM boundary_trials WHERE id = ?", (tid,)).rowcount > 0


def layer_activity() -> list[dict]:
    """每层最后一次有数据的时间和固件版本，给“执行节奏”用。延迟测量算 L2。"""
    out: dict[str, dict] = {k: {"layer": k, "last_at": None, "versions": []} for k in LAYERS}
    with _LOCK, _connect() as conn:
        rows = list(conn.execute("SELECT layer, created_at, version FROM records WHERE layer != ''"))
        rows += list(conn.execute("SELECT layer, created_at, version FROM measures WHERE layer != ''"))
        rows += [("L2", r[0], r[1]) for r in conn.execute(
            "SELECT created_at, version FROM latency_runs WHERE kind = 'measure'")]
    for layer, at, ver in rows:
        if layer not in out:
            continue
        o = out[layer]
        o["last_at"] = max(o["last_at"] or 0, at)
        if ver and ver not in o["versions"]:
            o["versions"].append(ver)
    return list(out.values())


def cadence(version: str, now: float | None = None) -> list[dict]:
    """按方案的执行节奏判断每层该不该跑：L2 / L4 / L5 每版本，L3 每周，L1 新硬件或新算法时。"""
    from .metrics import PER_VERSION_LAYERS, WEEKLY_LAYERS
    now = now or time.time()
    res = []
    for a in layer_activity():
        name, when = LAYERS[a["layer"]]
        if a["layer"] in PER_VERSION_LAYERS:
            due = bool(version) and version not in a["versions"]
            short = version.replace("robot-teleophu-d-", "").split(".2026")[0]
            why = f"{short} 还没有数据" if due else ("本版已有数据" if version else "没识别到固件版本")
        elif a["layer"] in WEEKLY_LAYERS:
            age = (now - a["last_at"]) / 86400 if a["last_at"] else None
            due = age is None or age > 7
            why = "从没跑过" if age is None else (f"上次 {age:.0f} 天前" if due else f"{age:.0f} 天内跑过")
        else:
            due = False
            why = "按需"
        res.append({**a, "name": name, "when": when, "due": due, "why": why})
    return res


def latest_version() -> str:
    with _LOCK, _connect() as conn:
        rows = list(conn.execute(
            "SELECT version, created_at FROM records WHERE version != '' UNION ALL "
            "SELECT version, created_at FROM measures WHERE version != '' UNION ALL "
            "SELECT version, created_at FROM latency_runs WHERE version != '' AND kind = 'measure' "
            "ORDER BY created_at DESC LIMIT 1"))
    return rows[0][0] if rows else ""
