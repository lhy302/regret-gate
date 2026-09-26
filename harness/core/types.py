"""统一数据结构定义（构建规范 §2/§3/§4/§5/§6/§8）。

`core/` 内部模块之间*只*通过这些数据结构通信，不通过全局状态。
本模块是依赖图的叶子：不 import 任何其他 harness 模块，也不 import `llm/`。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Iterator, Optional

# ---------------------------------------------------------------------------
# 枚举（用字符串常量，避免引入额外依赖，便于 JSONL 序列化）
# ---------------------------------------------------------------------------

RISK_ALLOW = "allow"
RISK_BUFFER = "buffer"
RISK_INTERCEPT = "intercept"
RISK_LEVELS = (RISK_ALLOW, RISK_BUFFER, RISK_INTERCEPT)
RISK_SEVERITY = {RISK_ALLOW: 0, RISK_BUFFER: 1, RISK_INTERCEPT: 2}

EFFECT_READ_ONLY = "read_only"
EFFECT_BUFFERABLE = "bufferable"
EFFECT_EXTERNALIZED = "externalized"
EFFECT_IRREVERSIBLE = "irreversible"

REV_INSERT = "insert"
REV_ERASE = "erase"
REV_REPLACE = "replace"

REV_STAGED = "staged"
REV_APPLIED = "applied"
REV_REJECTED = "rejected"

PENDING_PENDING = "pending"
PENDING_APPROVED = "approved"
PENDING_REJECTED = "rejected"
PENDING_REVISED = "revised"
PENDING_EXECUTED = "executed"
PENDING_AUDITED = "audited"

PROMPT_NONE = "none"
PROMPT_WEAK = "weak"
PROMPT_STRONG = "strong"
PROMPT_CONDITIONS = (PROMPT_NONE, PROMPT_WEAK, PROMPT_STRONG)

VERDICT_APPROVE = "approve"
VERDICT_REJECT = "reject"
VERDICT_NEEDS_REVISION = "needs_revision"
VERDICTS = (VERDICT_APPROVE, VERDICT_REJECT, VERDICT_NEEDS_REVISION)


def risk_severity(level: str) -> int:
    """allow < buffer < intercept，用于“取最严”。未知级别按最严处理。"""
    return RISK_SEVERITY.get(level, RISK_SEVERITY[RISK_INTERCEPT])


# ---------------------------------------------------------------------------
# LLM 抽象层数据结构（§2.1）
# ---------------------------------------------------------------------------


@dataclass
class SamplingConfig:
    """固定采样参数（铁律 10）。"""

    model: str = "fake-model-v1"
    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int = 4096
    seed: Optional[int] = 0
    provider: str = "fake"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: "TokenUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StreamChunk:
    """统一流式事件（§2.1）。"""

    kind: str  # text | tool_call_start | tool_call_delta | tool_call_end | done | error
    text: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    tool_args_delta: Optional[str] = None
    tool_args: Optional[dict] = None
    error: Optional[str] = None
    usage: Optional[TokenUsage] = None
    request_id: Optional[str] = None  # provider 请求 ID，用于可追溯（§11.5）

    def to_dict(self) -> dict:
        data = {
            "kind": self.kind,
            "text": self.text,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tool_args_delta": self.tool_args_delta,
            "tool_args": self.tool_args,
            "error": self.error,
        }
        if self.usage is not None:
            data["usage"] = self.usage.to_dict()
        if self.request_id is not None:
            data["request_id"] = self.request_id
        return data


# ---------------------------------------------------------------------------
# 工具与风险（§3/§4）
# ---------------------------------------------------------------------------


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    effect_type: str
    # risk_policy 见 configs/base.yaml 的 risk_policy 段；规则由 RiskPolicy 持有，
    # ToolSpec 只登记默认级别，具体判定由 RiskRouter 完成。
    default_level: str = RISK_INTERCEPT

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict = field(default_factory=dict)
    raw_args: str = ""
    malformed: bool = False
    malformed_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ParseResult:
    ok: bool
    call: Optional[ToolCall] = None
    failed_field: Optional[str] = None
    reason: Optional[str] = None
    raw_args: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "call": self.call.to_dict() if self.call else None,
            "failed_field": self.failed_field,
            "reason": self.reason,
            "raw_args": self.raw_args,
        }


@dataclass
class RiskDecision:
    level: str
    reason: str
    matched_rule: Optional[str] = None
    rule_id: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# PendingActions（§5）
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    tool_call_id: str
    tool_name: str
    ok: bool
    output: str = ""
    error: Optional[str] = None
    executed: bool = False
    dry_run: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PendingAction:
    id: str
    tool_call: ToolCall
    decision: RiskDecision
    status: str = PENDING_PENDING
    resolution_history: list = field(default_factory=list)
    audit_verdict: Optional[str] = None
    audit_issues: list = field(default_factory=list)
    result: Optional[ToolResult] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tool_call": self.tool_call.to_dict(),
            "risk": self.decision.level,
            "reason": self.decision.reason,
            "matched_rule": self.decision.matched_rule,
            "status": self.status,
            "resolution_history": list(self.resolution_history),
            "audit_verdict": self.audit_verdict,
            "audit_issues": list(self.audit_issues),
            "result": self.result.to_dict() if self.result else None,
        }


# ---------------------------------------------------------------------------
# RevisionStack（§6）
# ---------------------------------------------------------------------------


@dataclass
class Revision:
    id: str
    turn: int
    created_at: int
    target_text: str
    op: str
    payload: str = ""
    reason: str = ""
    status: str = REV_STAGED
    resolved_range: Optional[tuple] = None
    rejected_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "turn": self.turn,
            "created_at": self.created_at,
            "target_text": self.target_text,
            "op": self.op,
            "payload": self.payload,
            "reason": self.reason,
            "status": self.status,
            "resolved_range": list(self.resolved_range) if self.resolved_range else None,
            "rejected_reason": self.rejected_reason,
        }


@dataclass
class FinalizeResult:
    finalized_text: str
    applied: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    cache_break_point: int = 0

    def to_dict(self) -> dict:
        return {
            "finalized_text": self.finalized_text,
            "applied": [r.to_dict() for r in self.applied],
            "rejected": [r.to_dict() for r in self.rejected],
            "cache_break_point": self.cache_break_point,
        }


# ---------------------------------------------------------------------------
# ExternalAuditor（§8）
# ---------------------------------------------------------------------------


@dataclass
class AuditRequest:
    action: dict
    checklist: list
    context_snippet: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AuditResponse:
    verdict: str
    issues: list = field(default_factory=list)
    raw: str = ""
    parse_error: Optional[str] = None
    usage: Optional[TokenUsage] = None

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "issues": list(self.issues),
            "raw": self.raw,
            "parse_error": self.parse_error,
            "usage": self.usage.to_dict() if self.usage else None,
        }


# ---------------------------------------------------------------------------
# 任务（§12）
# ---------------------------------------------------------------------------


@dataclass
class ValidationSpec:
    kind: str  # compile | unit_test | regex | llm_judge | manual
    config: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Task:
    id: str
    category: str
    prompt: str
    validation: ValidationSpec
    timeout: float = 300.0
    expected_output_schema: Optional[dict] = None
    expected_error_prone_areas: list = field(default_factory=list)
    dangerous_operations: list = field(default_factory=list)
    baseline_difficulty: str = "medium"
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "category": self.category,
            "prompt": self.prompt,
            "validation": self.validation.to_dict(),
            "timeout": self.timeout,
            "expected_output_schema": self.expected_output_schema,
            "expected_error_prone_areas": list(self.expected_error_prone_areas),
            "dangerous_operations": list(self.dangerous_operations),
            "baseline_difficulty": self.baseline_difficulty,
            "metadata": dict(self.metadata),
        }


# ---------------------------------------------------------------------------
# 实验配置（§11.1）
# ---------------------------------------------------------------------------


@dataclass
class EnabledMechanisms:
    risk_router: bool = False
    tail_audit: bool = False
    external_auditor: bool = False
    revision_stack: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExperimentConfig:
    group: str
    prompt_condition: str = PROMPT_NONE
    enabled_mechanisms: EnabledMechanisms = field(default_factory=EnabledMechanisms)
    tasks: list = field(default_factory=list)
    runs_per_task: int = 1
    sampling: SamplingConfig = field(default_factory=SamplingConfig)
    timeout: float = 300.0
    sub_group: Optional[str] = None
    source_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "group": self.group,
            "prompt_condition": self.prompt_condition,
            "enabled_mechanisms": self.enabled_mechanisms.to_dict(),
            "tasks": list(self.tasks),
            "runs_per_task": self.runs_per_task,
            "sampling": self.sampling.to_dict(),
            "timeout": self.timeout,
            "sub_group": self.sub_group,
            "source_path": self.source_path,
        }


# ---------------------------------------------------------------------------
# 序列化工具
# ---------------------------------------------------------------------------


def to_jsonable(obj: Any) -> Any:
    """把 dataclass / tuple / 嵌套结构转换成可 JSON 序列化的对象。"""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if is_dataclass(obj) and not isinstance(obj, type):
        return to_jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    return str(obj)


def dumps(obj: Any) -> str:
    return json.dumps(to_jsonable(obj), ensure_ascii=False, sort_keys=True)


def iter_dict_items(obj: Any) -> Iterator[tuple]:
    """便利函数：可 JSON 化后按 key 迭代（测试用）。"""
    data = to_jsonable(obj)
    if isinstance(data, dict):
        yield from data.items()
