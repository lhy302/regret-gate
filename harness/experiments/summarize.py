"""命令行摘要：把 `metrics.jsonl` 聚合成一张紧凑表（人工快速查看 / 冒烟检查用）。

正式报告请用 `experiments/report.py`。

```
py -3.12 -m experiments.summarize --metrics runs/metrics.jsonl
py -3.12 -m experiments.summarize --metrics runs/metrics.jsonl --json out.json
```
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from experiments.analysis import build_analysis, load_metrics

GROUPS_ORDER = ["A", "B", "C", "D", "E", "F", "G", "H-none", "H-weak", "H-strong"]


def summarize(rows: list) -> str:
    analysis = build_analysis(rows)
    summaries = analysis["groups"]
    groups = [g for g in GROUPS_ORDER if g in summaries] + [
        g for g in sorted(summaries) if g not in GROUPS_ORDER
    ]
    header = (
        f"{'group':10s} {'runs':>4s} {'fail':>4s} {'syntax':>7s} {'logic':>6s} {'success':>8s} "
        f"{'sec':>5s} {'falseInt':>9s} {'tailMod':>8s} {'extEff':>7s} {'revHit':>7s} "
        f"{'intercepts':>10s} {'calls':>6s}"
    )
    lines = [header, "-" * len(header)]
    for group in groups:
        entry = summaries[group]
        metrics = entry["metrics"]

        def mean(field: str) -> str:
            value = (metrics.get(field) or {}).get("mean")
            return "-" if value is None else f"{value:.3f}"

        lines.append(
            f"{group:10s} {entry['runs']:4d} {entry['failures']:4d} {mean('syntax_error'):>7s} "
            f"{mean('logic_error'):>6s} {mean('task_success'):>8s} {mean('security_incident'):>5s} "
            f"{mean('false_intercept'):>9s} {mean('tail_audit_modified'):>8s} "
            f"{mean('external_audit_effective'):>7s} {mean('revision_hit_rate'):>7s} "
            f"{entry['intercepts_total']:>10d} {entry['budget']['max_llm_calls']:>6d}"
        )
    lines.append("")
    lines.append("假设判定：")
    for item in analysis["hypotheses"]:
        if item.get("falsified"):
            verdict = "证伪"
        elif item.get("supported"):
            verdict = "支持"
        elif item.get("unmeasurable"):
            verdict = "无法测量（本轮未触发该分支，不是证伪）"
        else:
            verdict = "不成立/无显著差异"
        lines.append(f"  {item['id']}: {verdict} —— {item['statement']}")
    lines.append("")
    lines.append("解读：")
    for note in analysis["interpretation"]:
        lines.append(f"  - {note}")
    lines.append("")
    perf = analysis["performance_acceptance"]
    lines.append(
        f"性能验收：max llm_calls={perf['max_llm_calls']}（≤10: {perf['llm_call_budget_ok']}）, "
        f"max audit bytes={perf['max_audit_log_bytes']}（<5MB: {perf['audit_size_ok']}）"
    )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="聚合 metrics.jsonl 并打印摘要")
    parser.add_argument("--metrics", default=os.path.join("runs", "metrics.jsonl"))
    parser.add_argument("--json", default=None, help="把结构化分析结果写到指定 JSON 文件")
    args = parser.parse_args(argv)

    rows = load_metrics(args.metrics)
    print(summarize(rows))
    if args.json:
        analysis = build_analysis(rows)
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(analysis, handle, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        print(f"\n结构化分析已写入 {os.path.abspath(args.json)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
