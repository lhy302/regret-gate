"""指标收集（构建规范 §13.1/§13.2）。

每次 run 结束后写一条 `metrics.jsonl`。字段严格对齐规范：

```
run_id, group, task_id, category,
syntax_error, logic_error, security_incident, false_intercept,
input_tokens, output_tokens, latency_ms,
tail_audit_modified, external_audit_effective, revision_hit_rate,
task_success, cache_break_ratio, error
```

另外补充规范要求但未列进 JSONL 形状的观测量（intercepts / matched_rule /
llm_calls / audit_log_bytes / tail_audit_* / external_audit_*），它们用于
§4.5 误拦截率按规则统计、§14.3 性能验收与 H1–H6 假设检验。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from core.types import to_jsonable

METRIC_FIELDS = (
    "run_id",
    "group",
    "task_id",
    "category",
    "prompt_condition",
    "syntax_error",
    "logic_error",
    "security_incident",
    "false_intercept",
    "input_tokens",
    "output_tokens",
    "latency_ms",
    "tail_audit_modified",
    "external_audit_effective",
    "revision_hit_rate",
    "task_success",
    "cache_break_ratio",
    "error",
    # 补充观测量
    "intercepts",
    "buffers",
    "false_intercept_rule",
    "intercept_rules",
    "llm_calls",
    "tail_audit_injections",
    "tail_audit_timed_out",
    "tail_audit_infrastructure_error",
    "external_audit_requests",
    "external_audit_rejected",
    "executed_actions",
    "revisions_applied",
    "revisions_rejected",
    "cache_break_point",
    "finalized_text_len",
    "validation_passed",
    "audit_log_bytes",
)


@dataclass
class RunMetrics:
    run_id: str
    group: str
    task_id: str
    category: str
    prompt_condition: str = "none"
    syntax_error: bool = False
    logic_error: bool = False
    security_incident: bool = False
    false_intercept: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    tail_audit_modified: bool = False
    external_audit_effective: bool = False
    revision_hit_rate: float = 0.0
    task_success: bool = False
    cache_break_ratio: float = 0.0
    error: Optional[str] = None
    intercepts: int = 0
    buffers: int = 0
    false_intercept_rule: Optional[str] = None
    intercept_rules: list = field(default_factory=list)
    llm_calls: int = 0
    tail_audit_injections: int = 0
    tail_audit_timed_out: bool = False
    tail_audit_infrastructure_error: bool = False
    external_audit_requests: int = 0
    external_audit_rejected: int = 0
    executed_actions: int = 0
    revisions_applied: int = 0
    revisions_rejected: int = 0
    cache_break_point: int = 0
    finalized_text_len: int = 0
    validation_passed: bool = False
    audit_log_bytes: int = 0

    def to_dict(self) -> dict:
        return to_jsonable(asdict(self))


def run_metrics_from_result(result) -> dict:
    """把 `HarnessResult` 转成一条 metrics 记录。"""
    pending = result.pending_actions or []
    intercepts = [p for p in pending if p.get("risk") == "intercept"]
    buffers = [p for p in pending if p.get("risk") == "buffer"]
    applied = [r for r in (result.revisions or []) if r.get("status") == "applied"]
    rejected = [r for r in (result.revisions or []) if r.get("status") == "rejected"]

    metrics = RunMetrics(
        run_id=result.run_id,
        group=result.group,
        task_id=result.task_id,
        category=result.category,
        prompt_condition=result.prompt_condition,
        syntax_error=bool(result.syntax_error),
        logic_error=bool(result.logic_error),
        security_incident=bool(result.security_incident),
        false_intercept=bool(result.false_intercept),
        input_tokens=int(result.token_usage.get("input_tokens", 0)),
        output_tokens=int(result.token_usage.get("output_tokens", 0)),
        latency_ms=int(result.latency_ms),
        tail_audit_modified=bool(result.tail_audit_modified),
        external_audit_effective=bool(result.external_audit_effective),
        revision_hit_rate=float(result.revision_hit_rate),
        task_success=bool(result.task_success),
        cache_break_ratio=float(result.cache_break_ratio),
        error=result.error,
        intercepts=len(intercepts),
        buffers=len(buffers),
        false_intercept_rule=result.false_intercept_rule,
        intercept_rules=[p.get("matched_rule") for p in intercepts],
        llm_calls=int(result.llm_calls),
        tail_audit_injections=int(result.tail_audit_injections),
        tail_audit_timed_out=bool(result.tail_audit_timed_out),
        tail_audit_infrastructure_error=bool(result.tail_audit_infrastructure_error),
        external_audit_requests=int(result.external_audit_requests),
        external_audit_rejected=int(result.external_audit_rejected),
        executed_actions=int(result.executed_actions),
        revisions_applied=len(applied),
        revisions_rejected=len(rejected),
        cache_break_point=int(result.cache_break_point),
        finalized_text_len=len(result.finalized_text or ""),
        validation_passed=bool((result.validation or {}).get("passed", False)),
    )
    return metrics.to_dict()


def metrics_header() -> list:
    return list(METRIC_FIELDS)


def aggregate(rows: list, field_name: str) -> dict:
    """对某个数值/布尔字段做分布统计（规范 §15.3：只报均值是错误）。"""
    import statistics

    values: list = []
    for row in rows:
        value = row.get(field_name)
        if value is None:
            continue
        if isinstance(value, bool):
            value = 1.0 if value else 0.0
        if isinstance(value, (int, float)):
            values.append(float(value))
    if not values:
        return {"n": 0, "mean": None, "median": None, "stdev": None, "min": None, "max": None}
    return {
        "n": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
        "min": min(values),
        "max": max(values),
    }
