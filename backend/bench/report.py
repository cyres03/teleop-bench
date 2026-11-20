"""视频延迟测试记录：按《专项测试记录模板》和写作规范，从测量结果自动生成飞书文档。

生成到 ~/.teleop-bench/reports/<时间>/：report.xml（飞书 DocxXML）、report.txt（给写作规范检查用的纯文本）、
fig/（对比图、逐次分布图、画面截帧）、video/（每次的 A|B 并排短视频）、tools/（脚本原件）。
发布时在该目录里调用 lark-cli docs +create，图片和附件按相对路径上传。
"""
from __future__ import annotations

import csv
import html
import json
import re
import shutil
import subprocess
import sys
import time
from html import escape as E
from pathlib import Path

from . import config, db
from .metrics import lower_regressed

TOOLS = Path(__file__).resolve().parent.parent / "tools"
FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
TARGET = {"近场": 100, "远程": 150}
ORDER = ["近场 H264", "近场 H265", "近场 JPEG", "远程 阿里云 H264", "远程 腾讯云 H264", "远程 腾讯云 H265"]


def target_of(link: str) -> int:
    return TARGET["近场"] if link.startswith("近场") else TARGET["远程"]


def _f(x: float) -> str:
    return f"{x:.1f}"


# ---------- 数据 ----------
def pick_runs(ids: list[int] | None = None, day: str | None = None) -> tuple[list[dict], dict | None]:
    """返回（测量结果，按时间正序；本批测量用的校准）。默认取今天的结果。"""
    runs = [r for r in db.list_latency_runs() if r["result"].get("ok") and "未核实" not in r["link"]]
    if ids:
        runs = [r for r in runs if r["id"] in ids]
    else:
        day = day or time.strftime("%Y-%m-%d")
        runs = [r for r in runs if time.strftime("%Y-%m-%d", time.localtime(r["created_at"])) == day]
    measures = sorted((r for r in runs if r["kind"] == "measure"), key=lambda r: r["created_at"])
    calibs = [r for r in db.list_latency_runs() if r["kind"] == "calib" and r["result"].get("ok")]
    calib = None
    if measures:
        before = [c for c in calibs if c["created_at"] <= measures[0]["created_at"]]
        calib = max(before, key=lambda c: c["created_at"]) if before else None
    return measures, calib


def group(measures: list[dict]) -> list[tuple[str, list[dict]]]:
    keys = sorted({r["link"] for r in measures}, key=lambda k: (ORDER.index(k) if k in ORDER else 99, k))
    return [(k, [r for r in measures if r["link"] == k]) for k in keys]


def cell_stats(rows: list[dict]) -> dict:
    means = [r["result"]["mean"] for r in rows]
    return {"n": len(rows), "mean": sum(means) / len(means), "p5": min(r["result"]["p5"] for r in rows),
            "p95": max(r["result"]["p95"] for r in rows), "pairs": sum(r["result"].get("pairs", 0) for r in rows)}


def prev_version(link: str, version: str) -> tuple[str, float] | None:
    """同一链路与编码在上一个固件版本的均值（版本按第一次出现的时间排）。"""
    if not version:
        return None
    runs = [r for r in db.list_latency_runs() if r["kind"] == "measure" and r["result"].get("ok") and r["version"]]
    first: dict[str, float] = {}
    for r in runs:
        first[r["version"]] = min(first.get(r["version"], r["created_at"]), r["created_at"])
    order = sorted(first, key=first.get)
    if version not in order:
        return None
    for v in reversed(order[:order.index(version)]):
        mine = [r["result"]["mean"] for r in runs if r["version"] == v and r["link"] == link]
        if mine:
            return v, sum(mine) / len(mine)
    return None


def regressed(mean: float, prev: tuple[str, float] | None) -> bool:
    return prev is not None and lower_regressed(prev[1], mean)


# ---------- 图 ----------
def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    if Path(FONT).exists():
        font_manager.fontManager.addfont(FONT)
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=FONT).get_name()
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def chart_compare(groups: list[tuple[str, list[dict]]], out: Path) -> None:
    plt = _plt()
    fig, ax = plt.subplots(figsize=(8, 0.62 * len(groups) + 1.2), dpi=150)
    for i, (k, rows) in enumerate(groups):
        s = cell_stats(rows)
        tgt = target_of(k)
        color = "#11806a" if s["mean"] <= tgt else "#c23b22"
        y = len(groups) - 1 - i
        ax.barh(y, s["mean"], height=0.5, color=color)
        ax.plot([s["p5"], s["p95"]], [y, y], color="#1f2933", lw=1.2)
        ax.plot([tgt, tgt], [y - 0.35, y + 0.35], color="#f26a1b", lw=2.2)
        ax.text(max(s["p95"], tgt) + 6, y, f"{_f(s['mean'])} ms（{s['n']} 次）", va="center", fontsize=9)
    ax.set_yticks(range(len(groups)), [k for k, _ in reversed(groups)])
    ax.set_xlabel("端到端视频延迟 ms（横条为均值，细线为 P5–P95，橙色为目标）")
    ax.set_xlim(0, max(320, max(cell_stats(r)["p95"] for _, r in groups) + 70))
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def chart_hist(run: dict, csv_path: Path, out: Path) -> bool:
    if not csv_path.exists():
        return False
    with open(csv_path, encoding="utf-8") as f:
        vals = [float(row[2]) for row in csv.reader(f) if row and re.match(r"-?\d", row[2])]
    if not vals:
        return False
    plt = _plt()
    fig, ax = plt.subplots(figsize=(7, 2.6), dpi=150)
    ax.hist(vals, bins=20, color="#52606d")
    r = run["result"]
    for x, c in ((r["mean"], "#1f2933"), (target_of(run["link"]), "#f26a1b")):
        ax.axvline(x, color=c, lw=1.8)
    ax.set_xlabel(f"{run['link']} {run['name']}：每次翻转的延迟 ms（黑线均值，橙线目标）")
    ax.set_ylabel("次数")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    return True


def _ffmpeg(args: list[str]) -> bool:
    ff = config.load()["ffmpeg"]
    p = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, timeout=180)
    return p.returncode == 0


def frames_and_clip(run: dict, fig: Path, video: Path) -> tuple[str | None, str | None]:
    """A|B 左眼并排：中间时刻截一帧，再转 10 s 小视频。原始录像太大，不进文档。"""
    files = run["result"].get("files") or []
    if len(files) != 2 or not all(Path(f).exists() for f in files):
        return None, None
    a, b = files
    filt = "[0:v]crop=iw/2:ih:0:0,scale=640:-2[a];[1:v]crop=iw/2:ih:0:0,scale=640:-2[b];[a][b]hstack"
    png = fig / f"{run['name']}_{run['id']}_画面.png"
    mp4 = video / f"{run['name']}_{run['id']}_A屏幕_B镜片.mp4"
    cam = run["result"].get("A", {})
    dur = cam.get("frames", 0) / cam["fps"] if cam.get("fps") else 30.0
    mid = dur / 2
    clip = min(10.0, dur)
    start = max(0.0, mid - clip / 2)
    ok1 = _ffmpeg(["-ss", f"{mid:.2f}", "-i", a, "-ss", f"{mid:.2f}", "-i", b, "-filter_complex", filt, "-frames:v", "1", str(png)])
    ok2 = _ffmpeg(["-ss", f"{start:.2f}", "-t", f"{clip:.2f}", "-i", a, "-ss", f"{start:.2f}", "-t", f"{clip:.2f}", "-i", b,
                   "-filter_complex", filt,
                   "-r", "30", "-c:v", "libx264", "-crf", "28", "-pix_fmt", "yuv420p", "-an", str(mp4)])
    return (png.name if ok1 else None), (mp4.name if ok2 else None)


# ---------- 文档 ----------
def p(t: str) -> str:
    return f"<p>{t}</p>"


def table(head: list[str], rows: list[list[str]]) -> str:
    cols = "".join("<col/>" for _ in head)
    h = "".join(f'<th vertical-align="top">{p(E(x))}</th>' for x in head)
    b = "".join("<tr>" + "".join(f'<td vertical-align="top">{c if c.startswith("<") else p(E(c))}</td>' for c in r)
                + "</tr>" for r in rows)
    return f"<table><colgroup>{cols}</colgroup><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>"


def ul(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{E(x)}</li>" for x in items) + "</ul>"


def img(path: str, cap: str) -> str:
    return f'<img path="@./{path}" caption="{E(cap)}"/>'


def card(path: str) -> str:
    return f'<figure view-type="Card"><source path="@./{path}"/></figure>'


def video_block(path: str) -> str:
    return f'<figure view-type="Preview"><source path="@./{path}"/></figure>'


def _robot_name(sn: str) -> str:
    return sn.rsplit("_", 1)[-1] if sn else "未识别"


def build(ids: list[int] | None = None, *, basis: str = "", note: str = "", day: str | None = None) -> dict:
    measures, calib = pick_runs(ids, day)
    if not measures:
        raise ValueError("没有可写进报告的测量结果")
    groups = group(measures)
    sn = measures[0]["robot"]
    version = measures[0]["version"]
    date = time.strftime("%m-%d", time.localtime(measures[0]["created_at"]))
    out = db.data_dir() / "reports" / time.strftime("%Y%m%d_%H%M%S")
    fig, vid, tools = out / "fig", out / "video", out / "tools"
    for d in (fig, vid, tools):
        d.mkdir(parents=True, exist_ok=True)
    for name in ("flash.html", "cam_capture.sh", "flash_latency.py"):
        shutil.copy2(TOOLS / name, tools / name)

    short_ver = re.sub(r"^robot-teleophu-d-", "", version).split(".2026")[0] if version else ""
    title = f"Oli 端到端视频延迟测试记录（{_robot_name(sn)}，{short_ver or '版本未记录'}，{date}）"
    x = [f"<title>{E(title)}</title>"]

    # 开头要点：每格一句
    lead = []
    for k, rows in groups:
        s = cell_stats(rows)
        tgt = target_of(k)
        verdict = "达标" if s["mean"] <= tgt else f"超出目标 {_f(s['mean'] - tgt)} ms"
        pv = prev_version(k, version)
        cmp_ = ""
        if pv:
            cmp_ = f"上一版 {pv[0]} 均值 {_f(pv[1])} ms，" + ("比上一版回退。" if regressed(s["mean"], pv) else "没有回退。")
        lead.append(f"{k}：均值 {_f(s['mean'])} ms，P5–P95 {s['p5']:.0f}–{s['p95']:.0f} ms（{s['n']} 次），"
                    f"目标 ≤{tgt} ms，{verdict}。{cmp_}")
    x.append(ul(lead))
    if basis:
        x.append(p(E(f"依据文档：{basis}")))
    x.append(p(E(f"设备：{sn or '未识别'}，{version or '固件未记录'}。")))
    x.append(p(E("测量：屏幕闪烁块加两台 USB 双目相机同时录像，时间戳用电脑时钟；相机 A 拍屏幕，相机 B 拍 Pico 镜片。"
                 "用遥操测试台录像和分析，脚本见最后“脚本与工具”。")))
    cam = measures[0]["result"].get("A", {})
    secs = round(cam.get("frames", 0) / cam["fps"]) if cam.get("fps") else 30
    pairs = [r["result"].get("pairs", 0) for r in measures]
    x.append(p(E(f"约定：延迟 = B 里的翻转时刻 − A 里对应的翻转时刻 − 校准值，单位 ms。每次录像 {secs} s，"
                 f"配上 {min(pairs)}–{max(pairs)} 次翻转；单次读数受帧间隔量化，以均值为准。")))
    if note:
        x.append(p(E(note)))

    t_rows = []
    if calib:
        t_rows.append([time.strftime("%H:%M", time.localtime(calib["created_at"])), "校准：两台相机都拍屏幕",
                       f"{_f(calib['result']['mean'])} ms，配上 {calib['result'].get('pairs', 0)} 次翻转"])
    for r in measures:
        t_rows.append([time.strftime("%H:%M", time.localtime(r["created_at"])), f"{r['link']}（{r['name']}）",
                       f"均值 {_f(r['result']['mean'])} ms"])
    x.append(table(["时间", "操作", "结果"], t_rows))

    x.append("<h1>测试准备</h1>")
    prep = [[f"连接的是 {_robot_name(sn)}（核对 SN）", "已确认" if sn else "未识别", sn or "—"],
            ["固件版本", "已记录" if version else "未记录", version or "—"]]
    if calib:
        cr = calib["result"]
        prep.append(["相机校准", "已完成", f"{_f(cr['mean'])} ms（±{_f(cr.get('ci95', 0))}），配上 {cr.get('pairs', 0)} 次翻转"])
    else:
        prep.append(["相机校准", "未找到", "测量前没有校准记录"])
    prep.append(["编码与链路", "已记录", "每次测量前按测量项设置，见下表“链路与编码”"])
    x.append(table(["项目", "状态", "备注"], prep))

    x.append("<h1>测试结果</h1>")
    res_rows, evidence = [], []
    for r in measures:
        res = r["result"]
        csv_path = Path(res.get("csv") or "/nonexistent")
        png, mp4 = frames_and_clip(r, fig, vid)
        hist = f"{r['name']}_{r['id']}_分布.png"
        has_hist = chart_hist(r, csv_path, fig / hist)
        tgt = target_of(r["link"])
        flips = min(res.get("A", {}).get("flips", 0), res.get("B", {}).get("flips", 0))
        res_rows.append([r["link"], r["name"], _f(res["mean"]), _f(res["median"]), f"{res['p5']:.0f}–{res['p95']:.0f}",
                         f"{res.get('pairs', 0)} / {flips}", f"≤{tgt}", "达标" if res["mean"] <= tgt else "超标",
                         video_block(f"video/{mp4}") if mp4 else "—"])
        head = f"{r['link']} {r['name']}：均值 {_f(res['mean'])} ms，P5–P95 {res['p5']:.0f}–{res['p95']:.0f} ms"
        block = [f"<h2>{E(head)}</h2>"]
        if has_hist:
            block.append(img(f"fig/{hist}", f"{r['link']} {r['name']}：每次翻转的延迟分布"))
        if png:
            block.append(img(f"fig/{png}", f"{r['name']} 录像中段画面：左为相机 A（屏幕），右为相机 B（Pico 镜片）"))
        if res.get("warning"):
            block.append(p(E(res["warning"])))
        evidence.append("".join(block))
    x.append(table(["链路与编码", "名称", "均值 ms", "中位数 ms", "P5–P95 ms", "配对 / 翻转", "目标 ms", "判定", "视频"], res_rows))

    x.append("<h1>对比汇总</h1>")
    chart_compare(groups, fig / "对比.png")
    x.append(img("fig/对比.png", "各链路与编码的端到端视频延迟"))
    x.append(table(["链路与编码", "次数", "均值 ms", "P5–P95 ms", "目标 ms", "判定"],
                   [[k, str(s["n"]), _f(s["mean"]), f"{s['p5']:.0f}–{s['p95']:.0f}", f"≤{target_of(k)}",
                     "达标" if s["mean"] <= target_of(k) else "超标"] for k, s in ((k, cell_stats(v)) for k, v in groups)]))

    x.append("<h1>逐项证据</h1>")
    x.extend(evidence)

    x.append("<h1>脚本与工具</h1>")
    x.append(table(["名称", "用途", "运行位置与用法", "原件"], [
        ["flash.html", "闪烁页：左右两半按 150–600 ms 随机间隔黑白反相翻转", "电脑外接屏全屏打开", card("tools/flash.html")],
        ["cam_capture.sh", "两台相机同时录像，时间戳用电脑时钟", "电脑：bash cam_capture.sh 名称 30 2560x720 相机A 相机B",
         card("tools/cam_capture.sh")],
        ["flash_latency.py", "找翻转、逐次配对、扣校准，输出均值和 P5–P95", "电脑：python flash_latency.py A.mkv B.mkv --calib 校准值",
         card("tools/flash_latency.py")],
        ["遥操测试台", "把上面三步做成界面，结果自动入库并生成本记录", "电脑：桌面图标“遥操测试台”", "—"],
    ]))

    x.append("<h1>结论</h1>")
    x.append(ul(lead))
    problems = [[f"{k} 视频延迟超出目标", f"均值 {_f(s['mean'])} ms，目标 ≤{target_of(k)} ms，P95 {s['p95']:.0f} ms",
                 "对比图、逐项证据", "未处理"] for k, s in ((k, cell_stats(v)) for k, v in groups) if s["mean"] > target_of(k)]
    for k, v in groups:
        s_, pv = cell_stats(v), prev_version(k, version)
        if regressed(s_["mean"], pv):
            problems.append([f"{k} 视频延迟比上一版回退", f"均值 {_f(s_['mean'])} ms，上一版 {pv[0]} 为 {_f(pv[1])} ms",
                             "对比图、逐项证据", "未处理"])
    if problems:
        x.append(table(["问题", "现象", "证据", "状态"], problems))

    xml = "".join(x)
    (out / "report.xml").write_text(xml, encoding="utf-8")
    text = re.sub(r"<(/?)(h1|h2|p|li|tr|title)[^>]*>", "\n", xml)
    text = re.sub(r"<[^>]+>", " ", text)
    (out / "report.txt").write_text(html.unescape(text), encoding="utf-8")
    lint = lint_text(out / "report.txt")
    meta = {"dir": str(out), "title": title, "runs": [r["id"] for r in measures], "lint": lint, "url": ""}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def lint_text(path: Path) -> dict:
    p_ = subprocess.run([sys.executable, str(TOOLS / "lint_report_style.py"), "--file", str(path)],
                        capture_output=True, text=True, timeout=60)
    out = p_.stdout.strip()
    m = re.search(r"错误 (\d+)，警告 (\d+)", out)
    return {"errors": int(m.group(1)) if m else -1, "warnings": int(m.group(2)) if m else -1, "output": out[-3000:]}


def preview_markdown(meta: dict) -> str:
    return (Path(meta["dir"]) / "report.txt").read_text(encoding="utf-8")


def publish(report_dir: str) -> dict:
    d = Path(report_dir)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    if meta["lint"]["errors"] != 0:
        raise ValueError("写作规范检查还有错误，先改好再发")
    lark = config.load().get("lark_cli") or shutil.which("lark-cli") or str(Path.home() / ".local/bin/lark-cli")
    p_ = subprocess.run([lark, "docs", "+create", "--parent-position", "my_library", "--title", meta["title"],
                         "--content", "@report.xml"], cwd=d, capture_output=True, text=True, timeout=900)
    (d / "create.log").write_text(p_.stdout + p_.stderr, encoding="utf-8")
    try:
        res = json.loads(p_.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"lark-cli 没有返回结果：{(p_.stderr or p_.stdout)[-300:]}")
    if not res.get("ok"):
        raise RuntimeError(f"创建飞书文档失败：{json.dumps(res.get('error'), ensure_ascii=False)[:300]}")
    doc = res["data"]["document"]
    meta["url"] = doc.get("url") or f"https://cwjgfm21di.feishu.cn/docx/{doc['document_id']}"
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta
