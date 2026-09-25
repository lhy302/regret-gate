"""ExternalAuditor：独立审核 Agent + 检查清单（构建规范 §8）。

铁律（§8.2）：
- **绝不传入完整对话历史**。只传：待审操作（工具名 + 参数）+ 预定义检查清单 +
  可选 `context_snippet`（最多 500 字符）；
- JSON 解析失败 → 先尝试从响应中提取 JSON 块；仍失败则按 `needs_revision` 处理，
  并附带原始响应（§8.4）；
- 审核模型必须与主模型不同（不同模型 / 至少不同 prompt 或 temperature，§8.5）；
- 成本控制：单次输入 ≤ 2000 token（按字符近似：4000 字符）、输出 ≤ 500 token
  （`max_tokens` 上限）、每回合审核次数 ≤ PendingActions 数量、超时 20s（§8.6）。
"""

from __future__ import annotations

import json
import re
from typing import Optional

from core.types import (
    VERDICT_NEEDS_REVISION,
    VERDICTS,
    AuditRequest,
    AuditResponse,
    TokenUsage,
)

DEFAULT_CHECKLIST = [
    "语法可解析",
    "导入完整",
    "边界条件已处理",
    "异常路径已覆盖",
    "权限与路径合法",
    "幂等性确认（如适用）",
    "命令作用域与目标确认",
    "不包含未声明的副作用",
]

AUDITOR_SYSTEM_PROMPT = (
    "你是一个独立审核 Agent，只依据给定的操作与检查清单做判断。"
    "你看不到对话历史，也不要去猜测意图。"
    "只输出严格 JSON，不要输出任何解释性文字或代码块围栏。"
)

MAX_CONTEXT_SNIPPET = 500
MAX_INPUT_CHARS = 4000  # ≈ 2000 token 上限的保守近似


class ExternalAuditor:
    def __init__(
        self,
        client,
        sampling,
        checklist: Optional[list] = None,
        max_output_tokens: int = 500,
        timeout: float = 20.0,
        audit_buffer: bool = False,
        context_snippet_chars: int = MAX_CONTEXT_SNIPPET,
    ):
        self.client = client
        self.sampling = sampling
        self.checklist = list(checklist or DEFAULT_CHECKLIST)
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout
        # §8.1：intercept 必审；buffer 是否触发可配置，默认不触发
        self.audit_buffer = audit_buffer
        self.context_snippet_chars = min(context_snippet_chars, MAX_CONTEXT_SNIPPET)
        self.requests_made = 0

    # -- 输入构造（§8.2） -------------------------------------------------

    def build_request(self, tool_call, context_snippet: Optional[str] = None) -> AuditRequest:
        snippet = context_snippet
        if snippet is not None:
            snippet = snippet[: self.context_snippet_chars]
        return AuditRequest(
            action={"tool_name": tool_call.name, "args": tool_call.args},
            checklist=list(self.checklist),
            context_snippet=snippet,
        )

    def render_prompt(self, request: AuditRequest) -> str:
        lines = [
            "待审操作：",
            json.dumps(request.action, ensure_ascii=False, sort_keys=True, indent=2),
            "",
            "检查清单：",
        ]
        lines += [f"- {item}" for item in request.checklist]
        if request.context_snippet:
            lines += ["", "相关上下文片段（最多 500 字符）：", request.context_snippet]
        lines += [
            "",
            "请只输出如下 JSON：",
            '{"verdict": "approve" | "reject" | "needs_revision", '
            '"issues": [{"severity": "high" | "medium" | "low", "description": "string", '
            '"suggestion": "string"}]}',
        ]
        prompt = "\n".join(lines)
        if len(prompt) > MAX_INPUT_CHARS:
            prompt = prompt[:MAX_INPUT_CHARS]
        return prompt

    # -- 审核 -------------------------------------------------------------

    def audit(self, tool_call, context_snippet: Optional[str] = None) -> AuditResponse:
        request = self.build_request(tool_call, context_snippet=context_snippet)
        prompt = self.render_prompt(request)
        self.requests_made += 1
        raw, usage, error = self._call(prompt)
        if error:
            return AuditResponse(
                verdict=VERDICT_NEEDS_REVISION,
                issues=[
                    {
                        "severity": "high",
                        "description": f"audit call failed: {error}",
                        "suggestion": "retry the audit or fall back to human review",
                    }
                ],
                raw=raw,
                parse_error=error,
                usage=usage,
            )
        return self.parse_response(raw, usage)

    def _call(self, prompt: str) -> tuple:
        sampling = self.sampling
        if hasattr(sampling, "model"):
            import dataclasses

            sampling = dataclasses.replace(
                sampling,
                max_tokens=min(getattr(sampling, "max_tokens", self.max_output_tokens), self.max_output_tokens),
            )
        try:
            text, usage = self.client.complete(
                system=AUDITOR_SYSTEM_PROMPT,
                prompt=prompt,
                sampling=sampling,
                timeout=self.timeout,
            )
            return text, usage or TokenUsage(), None
        except Exception as exc:  # noqa: BLE001 - 审核失败必须降级而不是崩溃
            return "", TokenUsage(), f"{type(exc).__name__}: {exc}"

    # -- 输出解析（§8.4） -------------------------------------------------

    @staticmethod
    def parse_response(raw: str, usage: Optional[TokenUsage] = None) -> AuditResponse:
        text = (raw or "").strip()
        payload = None
        parse_error = None

        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            payload = None

        if payload is None:
            block = _extract_json_block(text)
            if block is not None:
                try:
                    payload = json.loads(block)
                except json.JSONDecodeError as exc:
                    parse_error = f"json block parse failed: {exc}"
            else:
                parse_error = "no JSON object found in auditor response"

        if not isinstance(payload, dict):
            return AuditResponse(
                verdict=VERDICT_NEEDS_REVISION,
                issues=[
                    {
                        "severity": "high",
                        "description": f"auditor response not parseable: {parse_error}",
                        "suggestion": "re-run the audit or treat as needs_revision",
                    }
                ],
                raw=raw or "",
                parse_error=parse_error or "response is not a JSON object",
                usage=usage,
            )

        verdict = payload.get("verdict")
        if verdict not in VERDICTS:
            return AuditResponse(
                verdict=VERDICT_NEEDS_REVISION,
                issues=_normalize_issues(payload.get("issues")),
                raw=raw or "",
                parse_error=f"invalid verdict: {verdict!r}",
                usage=usage,
            )
        return AuditResponse(
            verdict=verdict,
            issues=_normalize_issues(payload.get("issues")),
            raw=raw or "",
            parse_error=None,
            usage=usage,
        )

    # -- 成本控制 ---------------------------------------------------------

    def can_audit(self, pending_count: int) -> bool:
        """每回合审核次数 ≤ PendingActions 数量（§8.6）。"""
        return self.requests_made < max(0, pending_count)

    def reset_turn(self) -> None:
        self.requests_made = 0

    def snapshot(self) -> dict:
        return {
            "requests_made": self.requests_made,
            "audit_buffer": self.audit_buffer,
            "timeout": self.timeout,
            "checklist": list(self.checklist),
        }


def _extract_json_block(text: str) -> Optional[str]:
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    start = text.find("{")
    while start != -1:
        depth = 0
        for idx in range(start, len(text)):
            char = text[idx]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : idx + 1]
        start = text.find("{", start + 1)
    return None


def _normalize_issues(issues) -> list:
    if not isinstance(issues, list):
        return []
    normalized = []
    for item in issues:
        if not isinstance(item, dict):
            continue
        severity = item.get("severity")
        if severity not in ("high", "medium", "low"):
            severity = "medium"
        normalized.append(
            {
                "severity": severity,
                "description": str(item.get("description", "")),
                "suggestion": str(item.get("suggestion", "")),
            }
        )
    return normalized
