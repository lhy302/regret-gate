"""工具注册表与工具调用解析（构建规范 §3）。

注册表由 harness 侧维护，**不信任模型声明**：模型在参数里写 `read_only: true`
不作为放行依据（§4.4 / 铁律 4）。

`tool_call_parser` 只做两件事：
1. 归一化 provider 的工具调用形态（OpenAI `tool_calls` / Anthropic `tool_use`）；
2. 按注册表 schema 校验。

解析失败**不抛异常**，返回 `ParseResult`（§3.3）。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from core.types import (
    EFFECT_BUFFERABLE,
    EFFECT_EXTERNALIZED,
    EFFECT_IRREVERSIBLE,
    EFFECT_READ_ONLY,
    REV_INSERT,
    REV_REPLACE,
    RISK_ALLOW,
    RISK_BUFFER,
    ParseResult,
    ToolCall,
    ToolSpec,
)

# ---------------------------------------------------------------------------
# 注册表（§3.2）
# ---------------------------------------------------------------------------

_READ_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "要读取的文件路径"},
        "encoding": {"type": "string", "description": "文本编码，默认 utf-8"},
    },
    "required": ["path"],
    "additionalProperties": False,
}

_WRITE_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "要写入的文件路径"},
        "content": {"type": "string", "description": "完整文件内容"},
        "mode": {"type": "string", "enum": ["overwrite", "append"]},
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}

_EXECUTE_SHELL_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "要执行的完整命令行"},
        "cwd": {"type": "string", "description": "工作目录"},
        "shell": {"type": "string", "enum": ["powershell", "cmd", "bash"]},
    },
    "required": ["command"],
    "additionalProperties": False,
}

_HTTP_REQUEST_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "请求 URL"},
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"]},
        "headers": {"type": "object"},
        "body": {"type": "string"},
    },
    "required": ["url", "method"],
    "additionalProperties": False,
}

_DRAFT_COMMIT_REVISION_SCHEMA = {
    "type": "object",
    "properties": {
        "target_text": {"type": "string", "description": "要修改的原文片段"},
        "op": {"type": "string", "enum": [REV_INSERT, "erase", REV_REPLACE]},
        "payload": {"type": "string", "description": "insert/replace 的新文本"},
        "reason": {"type": "string", "maxLength": 200},
    },
    "required": ["target_text", "op"],
    "additionalProperties": False,
}


def default_registry() -> dict:
    """返回本实验必须注册的工具（§3.2）。

    `default_level` 只是保守兜底；真正的判定在 `risk_router.RiskPolicy`。
    """
    specs = [
        ToolSpec(
            name="read_file",
            description="读取一个文件的内容（只读）。",
            parameters=_READ_FILE_SCHEMA,
            effect_type=EFFECT_READ_ONLY,
            default_level=RISK_ALLOW,
        ),
        ToolSpec(
            name="write_file",
            description="写入或覆盖一个文件（可缓冲的写操作）。",
            parameters=_WRITE_FILE_SCHEMA,
            effect_type=EFFECT_BUFFERABLE,
            default_level=RISK_BUFFER,
        ),
        ToolSpec(
            name="execute_shell",
            description="在本地 shell 中执行一条命令（可能不可逆）。",
            parameters=_EXECUTE_SHELL_SCHEMA,
            effect_type=EFFECT_IRREVERSIBLE,
            default_level=RISK_BUFFER,
        ),
        ToolSpec(
            name="http_request",
            description="发起一次 HTTP 请求（对外产生副作用）。",
            parameters=_HTTP_REQUEST_SCHEMA,
            effect_type=EFFECT_EXTERNALIZED,
            default_level=RISK_BUFFER,
        ),
        ToolSpec(
            name="draft.commit_revision",
            description=(
                "登记一条对本回合已写内容的修订（不改变任何外部状态）。"
                "target_text 是要修改的原文片段，op 是 insert/erase/replace，"
                "payload 是 insert/replace 的新文本。"
            ),
            parameters=_DRAFT_COMMIT_REVISION_SCHEMA,
            effect_type=EFFECT_READ_ONLY,
            default_level=RISK_ALLOW,
        ),
    ]
    return {s.name: s for s in specs}


def tools_payload(registry: Optional[dict] = None) -> list:
    """把注册表转成请求里 tools 字段的形态（OpenAI 风格，provider 适配器自行转换）。"""
    reg = registry if registry is not None else default_registry()
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }
        for spec in reg.values()
    ]


# ---------------------------------------------------------------------------
# schema 校验（子集实现：type / required / additionalProperties / enum / maxLength）
# ---------------------------------------------------------------------------

_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


def validate_args(schema: dict, args: Any) -> tuple:
    """返回 `(ok, failed_field, reason)`。只实现注册表用到的 schema 子集。"""
    if not isinstance(args, dict):
        return False, None, f"arguments must be a JSON object, got {type(args).__name__}"

    props = schema.get("properties", {})
    for field_name in schema.get("required", []):
        if field_name not in args:
            return False, field_name, "required field missing"

    if schema.get("additionalProperties") is False:
        for key in args:
            if key not in props:
                return False, key, "unexpected field"

    for key, value in args.items():
        rule = props.get(key)
        if not rule:
            continue
        expected = rule.get("type")
        if expected and expected in _TYPE_MAP:
            py_type = _TYPE_MAP[expected]
            if expected == "boolean":
                if not isinstance(value, bool):
                    return False, key, f"expected boolean, got {type(value).__name__}"
            elif expected in ("integer", "number"):
                if isinstance(value, bool) or not isinstance(value, py_type):
                    return False, key, f"expected {expected}, got {type(value).__name__}"
            elif not isinstance(value, py_type):
                return False, key, f"expected {expected}, got {type(value).__name__}"
        if "enum" in rule and value not in rule["enum"]:
            return False, key, f"value {value!r} not in enum {rule['enum']}"
        if rule.get("type") == "string" and "maxLength" in rule:
            if len(value) > rule["maxLength"]:
                return False, key, f"string longer than maxLength={rule['maxLength']}"
    return True, None, None


# ---------------------------------------------------------------------------
# 归一化（§2.2 provider 差异）
# ---------------------------------------------------------------------------


def _stringify_arguments(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(raw)


def normalize_tool_call(
    tool_name: Optional[str],
    raw_args: Any,
    tool_call_id: Optional[str] = None,
    index: int = 0,
) -> ToolCall:
    """把 provider 形态归一化为 `ToolCall`（解析 JSON，但不做 schema 校验）。"""
    raw_str = _stringify_arguments(raw_args)
    call_id = tool_call_id or f"call_{index}"
    if isinstance(raw_args, dict):
        args = dict(raw_args)
        return ToolCall(id=call_id, name=tool_name or "", args=args, raw_args=raw_str)
    if raw_args is None or raw_str.strip() == "":
        return ToolCall(
            id=call_id,
            name=tool_name or "",
            args={},
            raw_args=raw_str,
            malformed=True,
            malformed_reason="empty tool arguments",
        )
    try:
        parsed = json.loads(raw_str)
    except (json.JSONDecodeError, TypeError) as exc:
        return ToolCall(
            id=call_id,
            name=tool_name or "",
            args={},
            raw_args=raw_str,
            malformed=True,
            malformed_reason=f"invalid json: {exc}",
        )
    if not isinstance(parsed, dict):
        return ToolCall(
            id=call_id,
            name=tool_name or "",
            args={},
            raw_args=raw_str,
            malformed=True,
            malformed_reason=f"arguments must be object, got {type(parsed).__name__}",
        )
    return ToolCall(id=call_id, name=tool_name or "", args=parsed, raw_args=raw_str)


class ToolCallParser:
    """（§3.3）把归一化后的工具调用按注册表校验，失败返回结构化错误。"""

    def __init__(self, registry: Optional[dict] = None):
        self.registry = registry if registry is not None else default_registry()

    def parse(self, call: ToolCall) -> ParseResult:
        name = call.name or ""
        if call.malformed:
            return ParseResult(
                ok=False,
                call=call,
                failed_field=None,
                reason=call.malformed_reason or "malformed arguments",
                raw_args=call.raw_args,
            )
        spec = self.registry.get(name)
        if spec is None:
            return ParseResult(
                ok=False,
                call=call,
                failed_field="tool_name",
                reason=f"unknown tool: {name!r}",
                raw_args=call.raw_args,
            )
        ok, failed_field, reason = validate_args(spec.parameters, call.args)
        if not ok:
            return ParseResult(
                ok=False, call=call, failed_field=failed_field, reason=reason, raw_args=call.raw_args
            )
        return ParseResult(ok=True, call=call, raw_args=call.raw_args)
