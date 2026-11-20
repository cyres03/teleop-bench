"""LBMotion 报告的链路图：每次推杆 指令 / LBMotion 内部 / 实际 三根柱子。"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from ..wbt.report import _plt

BARS = [("cmd", "指令", "#9aa5b1"), ("lbm", "LBMotion 内部", "#4c78a8"), ("actual", "机器人实际", "#e45756")]


def hms(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def no_lbm(t: dict) -> bool:
    """前后、转向本该有 LBMotion 内部速度，录包里没有说明这次不是 LBMotion 在走。横移本来就没有这个字段。"""
    m = t.get("main") or {}
    return bool(m) and m.get("axis") != "y" and m.get("lbm") is None


def chain(trials: list[dict], out: Path, title: str) -> bool:
    ts = [t for t in trials if t.get("main") and not t.get("excluded")]
    groups = [(u, [t for t in ts if t["main"]["unit"] == u]) for u in ("m/s", "°/s")]
    groups = [(u, g) for u, g in groups if g]
    if not groups:
        return False
    plt = _plt()
    widths = [max(2, len(g)) for _, g in groups]
    fig, axs = plt.subplots(1, len(groups), figsize=(min(24, 3 + 1.6 * sum(widths)), 5),
                            gridspec_kw={"width_ratios": widths}, squeeze=False)
    for ax, (unit, g) in zip(axs[0], groups):
        x = np.arange(len(g))
        w = .26
        for k, (key, lab, c) in enumerate(BARS):
            v = [t["main"].get(key) for t in g]
            ax.bar(x + (k - 1) * w, [abs(i or 0) for i in v], w, label=lab, color=c)
            for xi, vi in zip(x, v):
                ax.text(xi + (k - 1) * w, abs(vi or 0), "—" if vi is None else f"{abs(vi):.2f}", ha="center", va="bottom", fontsize=8)
        ax.set_xticks(x, [f"{hms(t['started'])}\n{t['item']}" + ("\n工作包" if t.get("workpack") else "\n不计入" if no_lbm(t) else "") for t in g])
        ax.set_ylabel(f"速度绝对值 {unit}")
        ax.set_title("平移" if unit == "m/s" else "转向")
        ax.grid(axis="y", alpha=.3)
        ax.margins(y=.12)
    h, lab = axs[0][0].get_legend_handles_labels()
    fig.legend(h, lab, loc="upper center", ncol=3, fontsize=9, frameon=False, bbox_to_anchor=(.5, .93))
    fig.suptitle(title)
    fig.tight_layout(rect=(0, 0, 1, .9))
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return True
