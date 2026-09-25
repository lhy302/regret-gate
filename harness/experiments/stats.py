"""统计工具（构建规范 §13.3）。

全部为纯标准库实现，避免引入第三方统计依赖：
- 配对检验：同一任务在不同组之间配对（配对 t 检验，按任务先聚合成对均值）；
- 效应量：Cohen's d（配对差的 d_z 与池化 d 都给）；
- 置信区间：均值的 95% CI（t 分布）；
- 分层：按任务类别分组统计。
"""

from __future__ import annotations

import math
import statistics
from typing import Optional

# ---------------------------------------------------------------------------
# 分布函数（标准库实现）
# ---------------------------------------------------------------------------


def _betacf(a: float, b: float, x: float) -> float:
    tiny = 1e-30
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 200):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-12:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """正则化不完全贝塔函数 I_x(a, b)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_cdf(t: float, df: float) -> float:
    """Student's t 分布 CDF。"""
    if df <= 0:
        return float("nan")
    x = df / (df + t * t)
    prob = 0.5 * betainc(df / 2.0, 0.5, x)
    return 1.0 - prob if t > 0 else prob


def t_critical_two_sided(df: float, confidence: float = 0.95) -> float:
    """双侧临界值（二分法反解 CDF）。"""
    alpha = 1.0 - confidence
    target = 1.0 - alpha / 2.0
    lo, hi = 0.0, 1000.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if student_t_cdf(mid, df) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


# ---------------------------------------------------------------------------
# 描述统计
# ---------------------------------------------------------------------------


def describe(values: list) -> dict:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return {"n": 0, "mean": None, "median": None, "stdev": None, "min": None, "max": None, "ci95": None}
    mean = statistics.fmean(clean)
    stdev = statistics.stdev(clean) if len(clean) > 1 else 0.0
    ci = None
    if len(clean) > 1:
        half = t_critical_two_sided(len(clean) - 1) * stdev / math.sqrt(len(clean))
        ci = (mean - half, mean + half)
    return {
        "n": len(clean),
        "mean": mean,
        "median": statistics.median(clean),
        "stdev": stdev,
        "min": min(clean),
        "max": max(clean),
        "ci95": ci,
    }


# ---------------------------------------------------------------------------
# 配对检验
# ---------------------------------------------------------------------------


def paired_test(a_values: list, b_values: list, label_a: str = "A", label_b: str = "B") -> dict:
    """配对 t 检验 + Cohen's d + 95% CI（配对差）。

    `a_values` 与 `b_values` 必须一一对应（同一任务的配对观测）。
    """
    if len(a_values) != len(b_values):
        raise ValueError("paired samples must have equal length")
    diffs = [float(b) - float(a) for a, b in zip(a_values, b_values)]
    n = len(diffs)
    if n == 0:
        return {
            "n": 0, "label_a": label_a, "label_b": label_b, "mean_diff": None,
            "t": None, "p": None, "cohen_dz": None, "cohen_d_pooled": None,
            "ci95_diff": None, "note": "no paired observations",
        }
    mean_diff = statistics.fmean(diffs)
    sd_diff = statistics.stdev(diffs) if n > 1 else 0.0
    if sd_diff == 0.0:
        t_stat = 0.0 if mean_diff == 0.0 else float("inf")
        p_value = 1.0 if mean_diff == 0.0 else 0.0
        dz = 0.0 if mean_diff == 0.0 else float("inf")
    else:
        t_stat = mean_diff / (sd_diff / math.sqrt(n))
        p_value = 2.0 * (1.0 - student_t_cdf(abs(t_stat), n - 1))
        dz = mean_diff / sd_diff
    pooled_sd = math.sqrt((statistics.variance(a_values) + statistics.variance(b_values)) / 2.0) if n > 1 else 0.0
    d_pooled = (statistics.fmean(b_values) - statistics.fmean(a_values)) / pooled_sd if pooled_sd else None
    ci = None
    if n > 1:
        half = t_critical_two_sided(n - 1) * sd_diff / math.sqrt(n)
        ci = (mean_diff - half, mean_diff + half)
    return {
        "n": n,
        "label_a": label_a,
        "label_b": label_b,
        "mean_a": statistics.fmean(a_values),
        "mean_b": statistics.fmean(b_values),
        "mean_diff": mean_diff,
        "sd_diff": sd_diff,
        "t": t_stat,
        "p": p_value,
        "cohen_dz": dz,
        "cohen_d_pooled": d_pooled,
        "ci95_diff": ci,
        "note": None if n > 1 else "single pair: inferential stats are degenerate",
    }


# ---------------------------------------------------------------------------
# 分层与聚合
# ---------------------------------------------------------------------------


def group_by(rows: list, key_fields) -> dict:
    if isinstance(key_fields, str):
        key_fields = [key_fields]
    result: dict = {}
    for row in rows:
        key = tuple(row.get(field) for field in key_fields)
        result.setdefault(key, []).append(row)
    return result


def metric_values(rows: list, field: str) -> list:
    """把某个字段转成数值序列。

    注意：布尔必须**按真假转 0/1**，不能统一转成 1——否则所有布尔指标的均值都会
    恒等于 1.0，整张汇总表会看起来“全组都完美”，这是最危险的一类静默错误。
    """
    values = []
    for row in rows:
        value = row.get(field)
        if value is None:
            continue
        if isinstance(value, bool):
            values.append(1.0 if value else 0.0)
        else:
            values.append(float(value))
    return values


def task_level_means(rows: list, field: str) -> dict:
    """把同一 group 下同一 task 的多次 run 聚合成一个观测（配对的前提）。"""
    buckets = group_by(rows, ["task_id"])
    result = {}
    for (task_id,), items in buckets.items():
        values = metric_values(items, field)
        if values:
            result[task_id] = statistics.fmean(values)
    return result


def paired_compare(rows: list, group_a: str, group_b: str, field: str) -> dict:
    """按任务配对比较两个组在某个指标上的差异。"""
    rows_a = [r for r in rows if r.get("group") == group_a]
    rows_b = [r for r in rows if r.get("group") == group_b]
    means_a = task_level_means(rows_a, field)
    means_b = task_level_means(rows_b, field)
    shared = sorted(set(means_a) & set(means_b))
    if not shared:
        return {
            "n": 0, "label_a": group_a, "label_b": group_b, "field": field,
            "mean_diff": None, "t": None, "p": None, "cohen_dz": None,
            "cohen_d_pooled": None, "ci95_diff": None, "note": "no shared tasks",
        }
    a_values = [means_a[t] for t in shared]
    b_values = [means_b[t] for t in shared]
    result = paired_test(a_values, b_values, label_a=group_a, label_b=group_b)
    result["field"] = field
    result["shared_tasks"] = shared
    return result


def stratified_by_category(rows: list, field: str) -> dict:
    result = {}
    for (category,), items in group_by(rows, ["category"]).items():
        by_group = {}
        for (group,), group_rows in group_by(items, ["group"]).items():
            by_group[group] = describe(metric_values(group_rows, field))
        result[category] = by_group
    return result


def format_ci(ci: Optional[tuple]) -> str:
    if not ci:
        return "-"
    return f"[{ci[0]:.4f}, {ci[1]:.4f}]"


def format_p(p: Optional[float]) -> str:
    if p is None:
        return "-"
    if p == 0.0:
        return "<1e-16"
    return f"{p:.4g}"
