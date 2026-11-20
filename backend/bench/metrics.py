"""CPI / HPI 指标计算和格式化（Phil 10-30 口径）。

CPI = 总次数 ÷ issue 次数；CPI-F = 总次数 ÷ 摔倒次数；HPI / HPF 用累计小时代替次数。
issue 为 0 时写成“总数+”，例如 136+（136/0）。样本不超过 3 的只作单点参考。
"""
from __future__ import annotations

from dataclasses import dataclass

METRICS = ("CPI", "CPI-F", "HPI", "HPF")
TIME_METRICS = ("HPI", "HPF")
SMALL_SAMPLE = 3


def _num(value: float) -> str:
    """整数不带小数，其余保留 1 位，去掉末尾的 .0。"""
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


@dataclass(frozen=True)
class Indicator:
    metric: str
    total: float
    events: int
    value: float | None  # None 表示 0 次事件，此时只能说“至少 total”
    display: str  # 136+ / 2.4
    ratio: str  # (136/0)
    small_sample: bool

    @property
    def text(self) -> str:
        return f"{self.display}（{self.ratio[1:-1]}）"


def indicator(metric: str, total: float, events: int) -> Indicator:
    if metric not in METRICS:
        raise ValueError(f"未知指标 {metric}")
    if total < 0 or events < 0:
        raise ValueError("次数和时长不能为负")
    if metric not in TIME_METRICS and abs(total - round(total)) > 1e-9:
        raise ValueError(f"{metric} 的总次数必须是整数")
    if events == 0:
        value = None
        display = f"{_num(total)}+"
    else:
        value = total / events
        display = _num(value)
    small = metric not in TIME_METRICS and total <= SMALL_SAMPLE
    return Indicator(metric, total, events, value, display, f"({_num(total)}/{events})", small)


def meets_target(ind: Indicator, target: float | None) -> bool | None:
    """达标判定：越大越好。0 次事件时按“至少 total”比较。没有目标返回 None。"""
    if target is None:
        return None
    if ind.value is None:
        return ind.total >= target
    return ind.value >= target


# ---------- 《Oli 遥操跟随性能与稳定性测试方案》的规则 ----------
LAYERS = {
    "L1": ("分段精度", "新硬件 / 新算法时"),
    "L2": ("端到端跟随", "每版本"),
    "L3": ("稳定与可靠", "每周"),
    "L4": ("功能边界", "每版本 + 按需"),
    "L5": ("自动化回归", "每版本 / 过夜"),
}
PER_VERSION_LAYERS = ("L2", "L4", "L5")
WEEKLY_LAYERS = ("L3",)
FALL_METRICS = ("CPI-F", "HPF")
REGRESS_RATIO = 0.10  # 越小越好的测量值比上一版差 10% 以上算回退
MIN_TRIALS = 3  # 每设备每动作 ≥3 次


def f1_veto(metric: str, events: int, flat: bool) -> bool:
    """平地无障碍时不管操作员怎么做都不能摔：有摔倒就记 F1，一票否决。"""
    return bool(flat) and metric in FALL_METRICS and events > 0


def comparable(ind: Indicator) -> float:
    """越大越好；0 次事件时只能说“至少 total”，用 total 比较。"""
    return ind.value if ind.value is not None else ind.total


def higher_regressed(prev: Indicator, cur: Indicator) -> bool:
    """CPI / HPI 越大越好。本版有问题且比上一版小才算回退；本版 0 问题不算回退。"""
    return cur.events > 0 and comparable(cur) < comparable(prev)


def lower_regressed(prev: float, cur: float) -> bool:
    """延迟、摆幅、Ts 越小越好。"""
    return cur > prev * (1 + REGRESS_RATIO)


def status_of(*, veto: bool, meets: bool | None, regressed: bool, is_baseline: bool) -> tuple[str, str]:
    """台账一行的判定：F1 > 不达标 > 回退 > 达标 > 基线 > 无。返回 (代码, 文字)。"""
    if veto:
        return "f1", "F1 一票否决"
    if meets is False:
        return "fail", "不达标"
    if regressed:
        return "regress", "比上一版回退"
    if meets:
        return "pass", "达标"
    if is_baseline:
        return "baseline", "首测基线"
    return "none", "—"
