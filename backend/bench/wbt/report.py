"""WBT 回放测试的图和报告。报告给研发看：先总表，再按环节列差距清单（每条带时刻，能对着录包和视频找），最后方法。"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from . import analyze as an

FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
C = {"人": "#2563eb", "重定向": "#f59e0b", "机器人": "#dc2626"}


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


def _worst_part(res: dict) -> tuple[str, int]:
    """画哪个部位：人→重定向差距清单里最严重的部位，没有就取人活动幅度最大的那个。"""
    parts = res["人→重定向"]["部位"]
    for i in res["差距清单"]:
        if i["环节"] == "人→重定向" and i["项"] in parts:
            k = i["项"]
            return k[:-2], an.AXES.index(k[-2:])
    k = max(parts, key=lambda x: parts[x]["人幅度m"])
    return k[:-2], an.AXES.index(k[-2:])


def chart(res: dict, out: Path, title: str) -> None:
    plt = _plt()
    s = res["_series"]
    fig, ax = plt.subplots(2, 2, figsize=(13, 8.5))
    fig.suptitle(title, fontsize=14)
    a = ax[0, 0]
    a.plot(s["hn"][:, 1], s["hn"][:, 0], color=C["人"], label="人")
    a.plot(s["rn"][:, 1], s["rn"][:, 0], color=C["重定向"], ls="--", label="重定向")
    if "on" in s:
        a.plot(s["on"][:, 1], s["on"][:, 0], color=C["机器人"], label="机器人（腿部里程计）")
    a.set_title("骨盆轨迹俯视（起点朝上，左为 +）")
    a.set_xlabel("侧移 m")
    a.set_ylabel("前进 m")
    a.invert_xaxis()
    a.set_aspect("equal", adjustable="datalim")
    a.legend(fontsize=9)
    a.grid(alpha=.3)
    a = ax[0, 1]
    a.plot(s["th"], s["hyz"], color=C["人"], label="人")
    a.plot(s["tw"], s["ryz"], color=C["重定向"], ls="--", label="重定向")
    if "iyz" in s:
        a.plot(s["ti"], s["iyz"], color=C["机器人"], label="机器人（IMU）")
    a.set_title("偏航（相对起点，左转为 +）")
    a.set_xlabel("跟随开始后 s")
    a.set_ylabel("°")
    a.legend(fontsize=9)
    a.grid(alpha=.3)
    a = ax[1, 0]
    part, i = _worst_part(res)
    h, r = s["rel"][part]
    a.plot(s["th"], h[:, i], color=C["人"], label="人")
    a.plot(s["tw"], r[:, i], color=C["重定向"], ls="--", label="重定向")
    v = res["人→重定向"]["部位"][f"{part}{an.AXES[i]}"]
    a.set_title(f"{part}{an.AXES[i]}（相对骨盆）  幅度比 {v['比']}  相关 {v['相关']}  滞后 {v['滞后s']} s")
    a.set_xlabel("跟随开始后 s")
    a.set_ylabel("m")
    a.legend(fontsize=9)
    a.grid(alpha=.3)
    a = ax[1, 1]
    js = res["重定向→机器人"].get("关节") or []
    if js:
        j = js[0]
        cmd, act = s["joints"][j["关节"]]
        a.plot(s["tw"], cmd, color=C["重定向"], ls="--", label="指令（重定向）")
        a.plot(s["tj"], act, color=C["机器人"], label="实际（/joint/state）")
        a.set_title(f"跟踪误差最大的关节 {j['关节']}  均方根 {j['均方根deg']}°  滞后 {j['滞后s']} s")
        a.legend(fontsize=9)
    a.set_xlabel("跟随开始后 s")
    a.set_ylabel("°")
    a.grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)


def _n(v, unit="", nd=2):
    if v is None or v != v:
        return "—"
    return f"{v:.{nd}f}{unit}" if isinstance(v, float) else f"{v}{unit}"


def markdown(session: str, robot: str, version: str, runs: list[dict], folder: Path, history: list[dict] | None = None) -> str:
    if not runs:
        return "还没有结果。\n"
    out = [f"# 全身遥操回放测试 {session}", "",
           f"机器人 {robot or '—'}，版本 {version or '—'}。输入是 11-19 在 660 上录的 Pico 全身动捕基线包，每个版本播同一批包，输入完全相同。", "",
           "## 总表", "",
           "| # | 动作 | 控制器 | 跟随 s | 前进位移 m 人 / 重定向 / 机器人 | 侧移位移 m 人 / 重定向 / 机器人 | 偏航净转角 ° 人 / 重定向 / 机器人 | "
           "跟踪误差最大的关节 | 差距条数 人→重定向 / 重定向→机器人 |",
           "|-|-|-|-|-|-|-|-|-|"]
    for r in runs:
        x = r["result"]
        s1, s2 = x["人→重定向"], x["重定向→机器人"]
        d1, d2 = s1["位移"], s2.get("位移", {})
        rob = d2.get("机器人", {})
        y2 = s2.get("偏航", {})
        j = (s2.get("关节") or [{}])[0]
        n1 = sum(i["环节"] == "人→重定向" for i in x["差距清单"])
        n2 = sum(i["环节"] == "重定向→机器人" for i in x["差距清单"])
        tag = "（中途停止）" if r.get("aborted") else ""
        out.append(f"| {r['id']} | {r['item']}{tag} | {x.get('控制器') or '—'} | {x['follow_s']} | {d1['人']['前进']} / {d1['重定向']['前进']} / {_n(rob.get('前进'))} | "
                   f"{d1['人']['侧移']} / {d1['重定向']['侧移']} / {_n(rob.get('侧移'))} | "
                   f"{s1['偏航']['人净']} / {s1['偏航']['重定向净']} / {_n(y2.get('机器人净'), nd=1)} | "
                   f"{j.get('关节', '—')} {_n(j.get('均方根deg'), '°', 1)} | {n1} / {n2} |")
    if history:
        out += ["", "## 版本对比", "", "所有真机场次里播同一批基线包的结果，每个动作、版本、机器人取最新一次。完成率 = 机器人 ÷ 重定向（主方向；转身看右转最深的角度）。", "",
                "| 动作 | 版本 | 机器人 | 日期 | 主方向 | 重定向 | 机器人 | 完成率 | 偏航净差 ° | 关节误差均方根 中位 / 最大 ° | 差距条数 |",
                "|-|-|-|-|-|-|-|-|-|-|-|"]
        for h in history:
            u = f" {h['单位']}" if h["单位"] else ""
            out.append(f"| {h['item']} | {h['version']} | {h['robot']} | {h['date']} | {h['主方向']} | {_n(h['重定向'], u)} | {_n(h['机器人'], u)} | "
                       f"{_n(h['完成率'])} | {_n(h['偏航净差'], nd=1)} | {_n(h['关节误差中位'], nd=1)} / {_n(h['关节误差最大'], nd=1)} | {h['差距数']} |")
    for stage, who in (("重定向→机器人", "全身控制器（WBT）执行：重定向给的指令，机器人没做到"),
                       ("人→重定向", "重定向：人的动作，重定向没算对")):
        out += ["", f"## 差距清单：{stage}", "", f"{who}。按严重度排序，严重度 = 差距 ÷ 门限。时刻是跟随开始后的秒数。", "",
                "| # | 动作 | 项 | 前级 | 后级 | 差距 | 时刻 s | 严重度 |", "|-|-|-|-|-|-|-|-|"]
        rows = [(r, i) for r in runs for i in r["result"]["差距清单"] if i["环节"] == stage]
        rows.sort(key=lambda ri: -ri[1]["严重度"])
        for r, i in rows:
            out.append(f"| {r['id']} | {r['item']} | {i['项']} | {i['前级'] or '—'} | {i['后级'] or '—'} | {i['差距']} | "
                       f"{_n(i['时刻s'], nd=1)} | {i['严重度']} |")
        if not rows:
            out.append("| — | — | 没有超过门限的差距 | — | — | — | — | — |")
    out += ["", "## 逐次", ""]
    for r in runs:
        x = r["result"]
        s1, s2 = x["人→重定向"], x["重定向→机器人"]
        out += [f"### {r['id']} {r['item']}（{r['time']}）", ""]
        img = folder / "img" / f"{r['id']}.png"
        if img.exists():
            out += [f"![{r['item']}](img/{r['id']}.png)", ""]
        out += [f"- 录包 `{Path(r['bag']).name}`。",
                f"- 录包 MD5 `{r['md5']}`" + (f"，回放包 MD5 `{r['pack_md5']}`" if r.get("pack_md5") else "") + "。"]
        if r.get("note"):
            out.append(f"- {r['note']}。")
        out += [f"- 人→重定向：骨盆轨迹差均方根 {s1['轨迹差']['均方根m']} m，最大 {s1['轨迹差']['最大m']} m（{s1['轨迹差']['最大差时刻s']} s）。",
                f"- 人→重定向：偏航过程最大差 {s1['偏航']['曲线最大差']}°（{s1['偏航']['最大差时刻s']} s），滞后 {s1['偏航']['滞后s']} s。",
                f"- 人→重定向：骨盆高度幅度 人 {s1['骨盆高度']['人幅度m']} m，重定向 {s1['骨盆高度']['重定向幅度m']} m。"]
        if s2.get("偏航"):
            y = s2["偏航"]
            out.append(f"- 重定向→机器人：偏航过程最大差 {y['曲线最大差']}°（{y['最大差时刻s']} s），滞后 {y['滞后s']} s，相关 {y['相关']}。")
        out += ["", "| 部位（相对骨盆） | 人幅度 m | 重定向幅度 m | 幅度比 | 相关 | 滞后 s |", "|-|-|-|-|-|-|"]
        for k, v in s1["部位"].items():
            out.append(f"| {k} | {v['人幅度m']} | {v['重定向幅度m']} | {_n(v['比'])} | {_n(v['相关'], nd=3)} | {_n(v['滞后s'])} |")
        if s2.get("关节"):
            out += ["", "| 关节（误差大的前 8 个） | 指令幅度 ° | 误差均方根 ° | 最大误差 ° | 最大误差时刻 s | 滞后 s |", "|-|-|-|-|-|-|"]
            for j in s2["关节"][:8]:
                out.append(f"| {j['关节']} | {j['指令幅度deg']} | {j['均方根deg']} | {j['最大deg']} | {j['最大差时刻s']} | {_n(j['滞后s'])} |")
        out.append("")
    th = an.TH
    out += ["## 方法", "",
            "- 人：Pico 全身动捕 `/tf`（`pico_origin` → `/vr_body/*`）链式算到世界系。重定向：`/teleop/wb_cmd` 中 frame_id 为 world 的 anchors 和 joint_cmd。"
            "机器人：`/ImuData` 偏航、`/joint/state` 关节角，位移用腿部正运动学里程计（支撑脚不动积分，660 的 `/tf` base_link 没有位移）。",
            "- 控制器取跟随段里 `/teleop/session_state` 的 mode。",
            "- 只算跟随段（`/teleop/state` 为 1 的最长一段）。时间用录包接收时间，统一插值到 50 Hz 后比较。",
            "- 位移和偏航：相对跟随起点，前进/侧移按起点朝向分解，左为 +。部位：减去骨盆位置，按骨盆偏航转到身体系；幅度取 2%–98% 分位差。",
            "- 滞后：互相关找最佳对齐，正值表示后级晚。",
            f"- 列入差距清单的门限：位移差 ≥ {th['位移差m']} m，或前级位移 ≥ 0.5 m 时比值不在 {th['位移比'][0]}–{th['位移比'][1]}；"
            f"偏航净差 ≥ {th['偏航净差deg']}°；转动幅度（先转、回转，前级 ≥ 90°）比值不在 {th['转动比'][0]}–{th['转动比'][1]}；偏航过程最大差 ≥ {th['偏航曲线差deg']}°；骨盆轨迹最大偏离 ≥ {th['轨迹差m']} m；"
            f"部位幅度比不在 {th['幅度比'][0]}–{th['幅度比'][1]}（人幅度 ≥ 0.1 m 才算）、相关 < {th['相关']}、滞后 ≥ {th['滞后s']} s；"
            f"关节跟踪误差均方根 ≥ {th['关节均方根deg']}°。",
            "- 回放包：原始录包只留跟随段的 `/vr_cmd` 和人体 `/tf`，清掉 A/B/X/Y；开头补 2.5 s 保持首帧姿势（RG+A 进入遥操、按住握把），"
            "结尾补 1 s（RG+B 回 Walk、松开）；播放前把时间戳改成当前时间。"]
    return "\n".join(out) + "\n"
