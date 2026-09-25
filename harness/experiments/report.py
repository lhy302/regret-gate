"""Markdown 报告生成（构建规范 §13.4 / §16.8）。

报告必须包含：
- 每组指标汇总表（含均值 / 中位数 / 标准差 / 95% CI，不只均值）；
- 关键对比的配对检验结果（含效应量与置信区间，不只 p 值）；
- 失败案例分析；
- 缓存破坏点的分布（文本形式）；
- 结论与下一步建议。
"""

from __future__ import annotations

import json
import os
from typing import Optional

from experiments.analysis import build_analysis
from experiments.stats import format_ci, format_p

KEY_METRICS_ORDER = [
    ("syntax_error", "语法错误率"),
    ("logic_error", "逻辑错误率"),
    ("task_success", "任务成功率"),
    ("security_incident", "安全事故率"),
    ("false_intercept", "误拦截率(run级)"),
    ("tail_audit_modified", "尾部自审触发修改率"),
    ("external_audit_effective", "独立审核拦截有效率"),
    ("revision_hit_rate", "修订栈命中率"),
    ("input_tokens", "输入 token"),
    ("output_tokens", "输出 token"),
    ("latency_ms", "延迟(ms)"),
    ("cache_break_ratio", "缓存破坏点比例"),
]


def _fmt(value, digits: int = 4) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        if value != value:  # NaN
            return "-"
        if value == float("inf"):
            return "inf"
        return f"{value:.{digits}f}"
    return str(value)


def _group_table(summaries: dict) -> list:
    lines = [
        "| 组 | runs | 失败 | 语法错误率 | 逻辑错误率 | 任务成功率 | 安全事故率 | 误拦截率 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for group, entry in summaries.items():
        metrics = entry["metrics"]

        def m(field, key="mean"):
            return _fmt((metrics.get(field) or {}).get(key))

        lines.append(
            f"| {group} | {entry['runs']} | {entry['failures']} | {m('syntax_error')} | "
            f"{m('logic_error')} | {m('task_success')} | {m('security_incident')} | {m('false_intercept')} |"
        )
    return lines


def _distribution_table(summaries: dict) -> list:
    lines = [
        "| 组 | 指标 | n | 均值 | 中位数 | 标准差 | 95% CI | min | max |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for group, entry in summaries.items():
        for field, label in KEY_METRICS_ORDER:
            stat = (entry["metrics"] or {}).get(field) or {}
            if not stat.get("n"):
                continue
            lines.append(
                f"| {group} | {label} | {stat['n']} | {_fmt(stat['mean'])} | {_fmt(stat['median'])} | "
                f"{_fmt(stat['stdev'])} | {format_ci(stat.get('ci95'))} | {_fmt(stat['min'])} | {_fmt(stat['max'])} |"
            )
    return lines


def _pair_table(pairs: list) -> list:
    lines = [
        "| 对比 | 指标 | n(配对任务) | A 均值 | B 均值 | 差值(B-A) | t | p | Cohen's d_z | d(池化) | 差值 95% CI |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for entry in pairs:
        lines.append(
            f"| {entry.get('label', entry.get('label_a'))} ({entry.get('label_a')}→{entry.get('label_b')}) | "
            f"{entry.get('field')} | {entry.get('n')} | {_fmt(entry.get('mean_a'))} | {_fmt(entry.get('mean_b'))} | "
            f"{_fmt(entry.get('mean_diff'))} | {_fmt(entry.get('t'), 3)} | {format_p(entry.get('p'))} | "
            f"{_fmt(entry.get('cohen_dz'), 3)} | {_fmt(entry.get('cohen_d_pooled'), 3)} | "
            f"{format_ci(entry.get('ci95_diff'))} |"
        )
    return lines


def _cache_histogram(cache: dict) -> list:
    buckets = cache.get("buckets") or [0] * 10
    total = sum(buckets) or 1
    lines = ["```", "缓存破坏点比例 cache_break_ratio 分布（十分位）"]
    for idx, count in enumerate(buckets):
        lo = idx / 10
        hi = (idx + 1) / 10
        bar = "#" * int(round(40 * count / total))
        lines.append(f"[{lo:.1f},{hi:.1f}) {count:5d} {bar}")
    lines.append("```")
    return lines


def _hypothesis_table(hypotheses: list) -> list:
    lines = ["| 假设 | 陈述 | 结论 | 依据 |", "|---|---|---|---|"]
    for item in hypotheses:
        if item.get("falsified"):
            verdict = "证伪"
        elif item.get("supported"):
            verdict = "支持"
        elif item.get("unmeasurable"):
            verdict = "无法测量（本轮样本未触发该分支，不是证伪）"
        else:
            verdict = "不成立/无显著差异"
        if item.get("test"):
            test = item["test"]
            evidence = (
                f"n={test.get('n')}, diff={_fmt(test.get('mean_diff'))}, "
                f"p={format_p(test.get('p'))}, d_z={_fmt(test.get('cohen_dz'), 3)}"
            )
        elif item.get("measured"):
            evidence = json.dumps(item["measured"], ensure_ascii=False)
        else:
            evidence = "-"
        lines.append(f"| {item['id']} | {item['statement']} | {verdict} | {evidence} |")
    return lines


def _rule_table(summaries: dict) -> list:
    lines = ["| 组 | 规则 | intercept 次数 | 误拦截次数 | 误拦截率 | 待修 |", "|---|---|---|---|---|---|"]
    for group, entry in summaries.items():
        for rule, stat in (entry.get("rules") or {}).items():
            lines.append(
                f"| {group} | {rule} | {stat['intercepts']} | {stat['false']} | "
                f"{_fmt(stat['false_intercept_rate'])} | {'是' if stat['needs_rework'] else '否'} |"
            )
    if len(lines) == 2:
        lines.append("| - | （无 intercept 记录） | 0 | 0 | - | - |")
    return lines


def build_report(rows: list, meta: Optional[dict] = None, alpha: float = 0.05) -> str:
    meta = meta or {}
    analysis = build_analysis(rows, alpha=alpha)
    summaries = analysis["groups"]
    lines: list = []
    lines.append("# 后悔承诺门 · Harness 离线实验结果报告（FakeClient）")
    lines.append("")
    lines.append("> 本报告由 `experiments/report.py` 从 `runs/metrics.jsonl` 自动生成。")
    lines.append("> **数据来源为离线 `FakeClient`**，用于验证 harness 侧工程链路与指标口径，")
    lines.append("> **不构成对模型能力或后训练价值的任何结论**。真实结论必须在真实 API 上重跑。")
    lines.append("")
    lines.append("## 0. 运行元信息")
    lines.append("")
    lines.append(f"- 运行数：{analysis['runs']}")
    lines.append(f"- 失败 run 数：{analysis['failures']}（失败 run 全部保留在分母中）")
    lines.append(f"- 显著性水平 α：{alpha}")
    lines.append(f"- provider：{meta.get('provider', 'fake')}")
    lines.append(f"- 模型：{meta.get('model', 'fake-model-v1')}")
    lines.append(f"- 采样：temperature={meta.get('temperature', 0.0)}, top_p={meta.get('top_p', 1.0)}, "
                 f"seed={meta.get('seed', 0)}")
    lines.append(f"- 每组任务数：{meta.get('task_limit', 'n/a')}，每任务重复：{meta.get('runs_per_task', 1)}")
    if meta.get("generated_from"):
        lines.append(f"- 指标文件：`{meta['generated_from']}`")
    lines.append("")
    lines.append("确定性说明：同一配置 + 同一 FakeClient 脚本连跑两次，指标逐字节一致")
    lines.append("（由 `tests/test_end_to_end.py` 的确定性用例强制校验）。")
    lines.append("")
    lines.append("延迟口径说明：离线运行注入**固定步长时钟**（`DeterministicClock`，每步 500µs），")
    lines.append("因此本报告的 `latency_ms` 只反映“请求次数 × 固定步长”，**不代表任何真实性能**；")
    lines.append("真实延迟必须在真实 API 运行下测量。`cache_break_ratio` 为 payload 最早变化位置")
    lines.append("占 payload 长度的比例，仅作成本观测，不作为机制贡献。")
    lines.append("")

    lines.append("## 1. 每组指标汇总（均值）")
    lines.append("")
    lines.extend(_group_table(summaries))
    lines.append("")
    lines.append("## 2. 指标分布（均值 / 中位数 / 标准差 / 95% CI）")
    lines.append("")
    lines.extend(_distribution_table(summaries))
    lines.append("")
    lines.append("## 3. 关键对比的配对检验")
    lines.append("")
    lines.append("配对方式：同一 `task_id` 在不同组之间配对；同任务多次 run 先取组内均值（保证独立性）。")
    lines.append("")
    lines.extend(_pair_table(analysis["pairs"]))
    lines.append("")
    lines.append("## 4. 假设 H1–H6 判定")
    lines.append("")
    lines.extend(_hypothesis_table(analysis["hypotheses"]))
    lines.append("")
    lines.append("## 5. 误拦截率与规则级统计（§4.5）")
    lines.append("")
    fir = analysis["false_intercept"]
    lines.append(
        f"总体：总 intercept 次数 {fir['total_intercepts']}，其中误拦截 {fir['false_intercepts']}，"
        f"误拦截率 {_fmt(fir['rate'])}。"
    )
    lines.append("")
    lines.extend(_rule_table(summaries))
    lines.append("")
    lines.append("## 6. 成本与延迟")
    lines.append("")
    lines.append("| 组 | 平均 token | 相对 A 增量 | 增量比 | 平均延迟(ms) | 延迟增量(ms) |")
    lines.append("|---|---|---|---|---|---|")
    for group, entry in sorted(analysis["token_delta"]["by_group"].items()):
        latency = analysis["latency_delta"]["by_group"].get(group, {})
        lines.append(
            f"| {group} | {_fmt(entry['mean_tokens'], 1)} | {_fmt(entry['delta_tokens'], 1)} | "
            f"{_fmt(entry['delta_ratio'], 3)} | {_fmt(latency.get('mean_ms'), 1)} | "
            f"{_fmt(latency.get('delta_ms'), 1)} |"
        )
    lines.append("")
    lines.append("## 7. 缓存破坏点分布")
    lines.append("")
    lines.extend(_cache_histogram(analysis["cache_break"]))
    lines.append("")
    lines.append(f"分布统计：{json.dumps({k: v for k, v in analysis['cache_break'].items() if k != 'buckets'}, ensure_ascii=False)}")
    lines.append("")
    lines.append("## 8. 分层分析（按任务类别）")
    lines.append("")
    for field, label in (("syntax_error", "语法错误率"), ("logic_error", "逻辑错误率"),
                         ("task_success", "任务成功率")):
        lines.append(f"### {label}")
        lines.append("")
        lines.append("| 类别 | " + " | ".join(sorted(summaries)) + " |")
        lines.append("|" + "---|" * (len(summaries) + 1))
        for category, by_group in analysis["stratified"][field].items():
            cells = []
            for group in sorted(summaries):
                stat = by_group.get(group) or {}
                cells.append(f"{_fmt(stat.get('mean'))} (n={stat.get('n', 0)})")
            lines.append(f"| {category} | " + " | ".join(cells) + " |")
        lines.append("")
    lines.append("## 9. 失败与未成功案例分析")
    lines.append("")
    failure_cases = analysis["failure_cases"]
    if not failure_cases:
        lines.append("无失败案例。")
    else:
        lines.append("| run_id | 组 | 任务 | 类别 | error | 语法错误 | 逻辑错误 | intercepts | 修订命中率 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for case in failure_cases:
            lines.append(
                f"| {case['run_id']} | {case['group']} | {case['task_id']} | {case['category']} | "
                f"{case['error'] or '-'} | {case['syntax_error']} | {case['logic_error']} | "
                f"{case['intercepts']} | {_fmt(case['revision_hit_rate'], 3)} |"
            )
        lines.append("")
        lines.append(f"（最多列出 50 条；共 {analysis['failures']} 个失败 run、"
                     f"{max(0, analysis['runs'] - sum(1 for r in rows if r.get('task_success')))} 个未成功 run。）")
    lines.append("")
    lines.append("## 10. 性能与预算验收（§14.3）")
    lines.append("")
    perf = analysis["performance_acceptance"]
    lines.append(f"- 单 run 最大 API 调用次数：{perf['max_llm_calls']}（要求 ≤ 10）："
                 f"{'通过' if perf['llm_call_budget_ok'] else '不通过'}")
    lines.append(f"- 单 run 审计日志最大字节：{perf['max_audit_log_bytes']}（要求 < 5 MB）："
                 f"{'通过' if perf['audit_size_ok'] else '不通过'}")
    lines.append(f"- 单 run 最大延迟（离线为固定步长时钟值，不代表真实性能）："
                 f"{perf['max_latency_ms']} ms（要求 < 任务 timeout）")
    lines.append("")
    lines.append("## 11. 结论与下一步建议")
    lines.append("")
    for note in analysis["interpretation"]:
        lines.append(f"- {note}")
    lines.append("")
    lines.append("**下一步（按优先级）**")
    lines.append("")
    lines.append("1. 用真实 API（固定模型版本 / temperature / seed）重跑 A–H，替换本报告的离线数据；")
    lines.append("2. 对 `unit_test` 类任务补齐真实测试文件，使逻辑错误率具备机器判定口径；")
    lines.append("3. 按 §4.5 的规则级误拦截率重修 RiskPolicy（>20% 的规则必须重做）；")
    lines.append("4. H 组扩到 `runs_per_task≥3` 并纳入分层分析，提高能力边界结论的置信度；")
    lines.append("5. 真实 API 场景补齐 `request_id` 记录与重试统计（本离线报告不涉及）。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("> 生成脚本：`experiments/report.py`；指标文件：`runs/metrics.jsonl`；"
                 "审计日志：`runs/audit/<run_id>.jsonl`。")
    return "\n".join(lines) + "\n"


def write_report(rows: list, path: str, meta: Optional[dict] = None, alpha: float = 0.05) -> str:
    text = build_report(rows, meta=meta, alpha=alpha)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return text


def main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="从 metrics.jsonl 生成 Markdown 报告")
    parser.add_argument("--metrics", default=os.path.join("runs", "metrics.jsonl"))
    parser.add_argument("--out", default="report.md")
    parser.add_argument("--meta", default=None, help="运行元信息 JSON 文件（可选）")
    parser.add_argument("--alpha", type=float, default=0.05)
    args = parser.parse_args(argv)

    from experiments.analysis import load_metrics

    rows = load_metrics(args.metrics)
    meta = {}
    if args.meta and os.path.isfile(args.meta):
        with open(args.meta, "r", encoding="utf-8") as handle:
            meta = json.load(handle)
    meta.setdefault("generated_from", args.metrics)
    write_report(rows, args.out, meta=meta, alpha=args.alpha)
    print(f"报告已写入 {os.path.abspath(args.out)}（{len(rows)} runs）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
