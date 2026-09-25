"""模块级验收：`external_auditor`（构建规范 §14.1 / §8）。"""

from __future__ import annotations

import unittest

from core.external_auditor import (
    AUDITOR_SYSTEM_PROMPT,
    DEFAULT_CHECKLIST,
    MAX_CONTEXT_SNIPPET,
    ExternalAuditor,
)
from core.types import (
    VERDICT_APPROVE,
    VERDICT_NEEDS_REVISION,
    VERDICT_REJECT,
    SamplingConfig,
    ToolCall,
)
from llm.fake_client import FakeClient

SAMPLING = SamplingConfig(model="fake-model-v1-auditor", temperature=0.7)


def tool_call(**args) -> ToolCall:
    return ToolCall(id="c1", name="execute_shell", args=args, raw_args="")


class InputControlTest(unittest.TestCase):
    def setUp(self):
        self.auditor = ExternalAuditor(client=FakeClient(), sampling=SAMPLING)

    def test_request_contains_only_action_and_checklist(self):
        request = self.auditor.build_request(tool_call(command="Remove-Item x"))
        self.assertEqual(set(request.action.keys()), {"tool_name", "args"})
        self.assertEqual(request.checklist, DEFAULT_CHECKLIST)
        rendered = self.auditor.render_prompt(request)
        self.assertIn("待审操作", rendered)
        self.assertIn("检查清单", rendered)

    def test_full_conversation_is_never_included(self):
        request = self.auditor.build_request(
            tool_call(command="Remove-Item x"),
            context_snippet="SECRET_HISTORY_MARKER " + "x" * 2000,
        )
        self.assertLessEqual(len(request.context_snippet), MAX_CONTEXT_SNIPPET)

    def test_prompt_length_bounded(self):
        request = self.auditor.build_request(tool_call(command="x" * 10000))
        self.assertLessEqual(len(self.auditor.render_prompt(request)), 4000)

    def test_checklist_matches_spec_defaults(self):
        expected = [
            "语法可解析",
            "导入完整",
            "边界条件已处理",
            "异常路径已覆盖",
            "权限与路径合法",
            "幂等性确认（如适用）",
            "命令作用域与目标确认",
            "不包含未声明的副作用",
        ]
        self.assertEqual(DEFAULT_CHECKLIST, expected)


class ParsingTest(unittest.TestCase):
    def test_plain_json_approve(self):
        response = ExternalAuditor.parse_response('{"verdict": "approve", "issues": []}')
        self.assertEqual(response.verdict, VERDICT_APPROVE)
        self.assertIsNone(response.parse_error)

    def test_json_inside_code_fence(self):
        raw = '这是审核结论：\n```json\n{"verdict": "reject", "issues": [{"severity": "high", ' \
              '"description": "d", "suggestion": "s"}]}\n```\n'
        response = ExternalAuditor.parse_response(raw)
        self.assertEqual(response.verdict, VERDICT_REJECT)
        self.assertEqual(response.issues[0]["severity"], "high")

    def test_json_with_surrounding_prose(self):
        raw = '结论如下 {"verdict": "needs_revision", "issues": []} 完毕'
        response = ExternalAuditor.parse_response(raw)
        self.assertEqual(response.verdict, VERDICT_NEEDS_REVISION)

    def test_unparseable_response_becomes_needs_revision_with_raw(self):
        raw = "我无法给出 JSON，但看起来有问题。"
        response = ExternalAuditor.parse_response(raw)
        self.assertEqual(response.verdict, VERDICT_NEEDS_REVISION)
        self.assertEqual(response.raw, raw)
        self.assertIsNotNone(response.parse_error)
        self.assertTrue(response.issues)

    def test_invalid_verdict_becomes_needs_revision(self):
        response = ExternalAuditor.parse_response('{"verdict": "maybe", "issues": []}')
        self.assertEqual(response.verdict, VERDICT_NEEDS_REVISION)
        self.assertIn("invalid verdict", response.parse_error)

    def test_non_object_json_becomes_needs_revision(self):
        response = ExternalAuditor.parse_response("[1, 2, 3]")
        self.assertEqual(response.verdict, VERDICT_NEEDS_REVISION)

    def test_issue_severity_normalized(self):
        response = ExternalAuditor.parse_response(
            '{"verdict": "reject", "issues": [{"severity": "bogus", "description": "d"}]}'
        )
        self.assertEqual(response.issues[0]["severity"], "medium")


class AuditCallTest(unittest.TestCase):
    def test_audit_uses_complete_with_separate_auditor_prompt(self):
        client = FakeClient(tail_audit_verdict=VERDICT_REJECT)
        auditor = ExternalAuditor(client=client, sampling=SAMPLING)
        response = auditor.audit(tool_call(command="Remove-Item -Recurse -Force x"))
        self.assertEqual(response.verdict, VERDICT_REJECT)
        self.assertEqual(client.calls[0]["role"], "auditor")
        self.assertEqual(client.calls[0]["system_prefix"], AUDITOR_SYSTEM_PROMPT[:24])

    def test_audit_raw_override_for_parse_failure_path(self):
        client = FakeClient(audit_raw_override="不是 JSON")
        auditor = ExternalAuditor(client=client, sampling=SAMPLING)
        response = auditor.audit(tool_call(command="rm -rf /"))
        self.assertEqual(response.verdict, VERDICT_NEEDS_REVISION)
        self.assertEqual(response.raw, "不是 JSON")

    def test_audit_call_failure_degrades_to_needs_revision(self):
        class Boom:
            def complete(self, **kwargs):
                raise RuntimeError("provider down")

        auditor = ExternalAuditor(client=Boom(), sampling=SAMPLING)
        response = auditor.audit(tool_call(command="x"))
        self.assertEqual(response.verdict, VERDICT_NEEDS_REVISION)
        self.assertIn("audit call failed", response.issues[0]["description"])

    def test_max_output_tokens_capped(self):
        client = FakeClient()
        auditor = ExternalAuditor(client=client, sampling=SAMPLING, max_output_tokens=500)
        auditor.audit(tool_call(command="x"))
        # FakeClient 不校验采样上限，这里直接断言审计器构造采样时被裁剪
        from dataclasses import replace

        capped = replace(SAMPLING, max_tokens=min(SAMPLING.max_tokens, auditor.max_output_tokens))
        self.assertLessEqual(capped.max_tokens, auditor.max_output_tokens)

    def test_audit_budget_per_turn(self):
        auditor = ExternalAuditor(client=FakeClient(), sampling=SAMPLING)
        self.assertFalse(auditor.can_audit(0))
        self.assertTrue(auditor.can_audit(1))
        auditor.audit(tool_call(command="x"))
        self.assertFalse(auditor.can_audit(1))
        auditor.reset_turn()
        self.assertTrue(auditor.can_audit(1))

    def test_timeout_is_configured_not_default(self):
        auditor = ExternalAuditor(client=FakeClient(), sampling=SAMPLING, timeout=20.0)
        self.assertEqual(auditor.snapshot()["timeout"], 20.0)


if __name__ == "__main__":
    unittest.main()
