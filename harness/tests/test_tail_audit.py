"""模块级验收：`tail_audit`（构建规范 §14.1 / §7）。"""

from __future__ import annotations

import unittest

from core.tail_audit import TAIL_AUDIT_PROMPT, TailAudit
from core.types import SamplingConfig
from llm.fake_client import FakeClient

SAMPLING = SamplingConfig(model="fake-model-v1")


class OneInjectionTest(unittest.TestCase):
    def test_injects_exactly_once_per_turn(self):
        audit = TailAudit()
        audit.start_turn(0)
        self.assertTrue(audit.should_inject())
        self.assertTrue(audit.mark_injected())
        self.assertFalse(audit.should_inject())
        self.assertFalse(audit.mark_injected())
        self.assertEqual(audit.inject_count, 1)

    def test_new_turn_resets_flag(self):
        audit = TailAudit()
        audit.start_turn(0)
        audit.mark_injected()
        audit.start_turn(1)
        self.assertTrue(audit.should_inject())
        self.assertEqual(audit.inject_count, 0)

    def test_second_injection_attempt_returns_false_and_does_not_call_model(self):
        client = FakeClient(script=[[{"kind": "text", "text": "ok"}, {"kind": "done"}]])
        audit = TailAudit()
        audit.start_turn(0)
        audit.mark_injected()
        result = audit.run(client, [{"role": "user", "content": "hi"}], [], SAMPLING, 30.0)
        self.assertFalse(result["injected"])
        self.assertTrue(result["loop_limit_hit"])
        self.assertEqual(client.call_count, 0)

    def test_extra_loop_budget_enforced(self):
        audit = TailAudit(max_extra_loops=3)
        self.assertTrue(audit.allow_extra_loop())
        self.assertTrue(audit.allow_extra_loop())
        self.assertTrue(audit.allow_extra_loop())
        self.assertFalse(audit.allow_extra_loop())


class InjectionFormatTest(unittest.TestCase):
    def test_prompt_matches_spec_text(self):
        expected = (
            "[尾部强制自审]\n"
            "本回合输出已完成。请通读最终输出字符串，特别检查：\n"
            "1. 尾部是否包含将被执行的命令、写操作、外部调用；\n"
            "2. 这些操作的参数、路径、作用域是否正确；\n"
            "3. 若发现错误，调用 draft.commit_revision 登记修改；\n"
            "4. 若无误，以纯文本结束本回合。"
        )
        self.assertEqual(TAIL_AUDIT_PROMPT.strip(), expected)

    def test_injected_as_user_role_and_does_not_mutate_input(self):
        audit = TailAudit()
        original = [{"role": "user", "content": "task"}]
        injected = audit.append_prompt(original)
        self.assertEqual(len(original), 1)
        self.assertEqual(len(injected), 2)
        self.assertEqual(injected[-1]["role"], "user")
        self.assertIn("[尾部强制自审]", injected[-1]["content"])

    def test_never_uses_system_role(self):
        audit = TailAudit()
        injected = audit.append_prompt([])
        self.assertNotEqual(injected[-1]["role"], "system")


class ResponseHandlingTest(unittest.TestCase):
    def _messages(self):
        return [{"role": "user", "content": "写一个长函数"}]

    def test_revision_tool_call_returned_to_caller(self):
        client = FakeClient(
            script=[
                [
                    {"kind": "tool_call_start", "tool_call_id": "c1", "tool_name": "draft.commit_revision"},
                    {"kind": "tool_call_delta", "tool_call_id": "c1", "tool_args_delta": '{"target_text": "a",'},
                    {"kind": "tool_call_delta", "tool_call_id": "c1", "tool_args_delta": ' "op": "replace", "payload": "b"}'},
                    {"kind": "tool_call_end", "tool_call_id": "c1", "tool_name": "draft.commit_revision"},
                    {"kind": "done"},
                ],
                [{"kind": "text", "text": "（无新增修订）"}, {"kind": "done"}],
            ]
        )
        audit = TailAudit()
        audit.start_turn(0)
        result = audit.run(client, self._messages(), [], SAMPLING, 30.0)
        self.assertTrue(result["injected"])
        self.assertFalse(result["timed_out"])
        self.assertEqual(len(result["tool_calls"]), 1)
        self.assertEqual(result["tool_calls"][0].args["payload"], "b")

    def test_plain_text_ends_the_window(self):
        client = FakeClient(script=[[{"kind": "text", "text": "未发现问题。"}, {"kind": "done"}]])
        audit = TailAudit()
        audit.start_turn(0)
        result = audit.run(client, self._messages(), [], SAMPLING, 30.0)
        self.assertEqual(result["text"], "未发现问题。")
        self.assertEqual(result["tool_calls"], [])
        self.assertEqual(client.call_count, 1)

    def test_timeout_recorded_and_no_exception(self):
        client = FakeClient(
            script=[[{"kind": "text", "text": "慢"}, {"kind": "done"}]],
            sleep_per_chunk=0.02,
        )
        audit = TailAudit(timeout=0.0001)
        audit.start_turn(0)
        result = audit.run(client, self._messages(), [], SAMPLING, 30.0)
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["injected"])

    def test_loop_limit_hit_stops_generation(self):
        script = []
        for _ in range(10):
            script.append(
                [
                    {"kind": "tool_call_start", "tool_call_id": "c", "tool_name": "draft.commit_revision"},
                    {"kind": "tool_call_end", "tool_call_id": "c", "tool_name": "draft.commit_revision",
                     "tool_args": {"target_text": "a", "op": "replace", "payload": "b"}},
                    {"kind": "done"},
                ]
            )
        client = FakeClient(script=script)
        audit = TailAudit(max_extra_loops=3)
        audit.start_turn(0)
        result = audit.run(client, self._messages(), [], SAMPLING, 30.0)
        self.assertTrue(result["loop_limit_hit"])
        self.assertLessEqual(client.call_count, 3)

    def test_prompt_not_leaked_into_returned_text(self):
        client = FakeClient(script=[[{"kind": "text", "text": "无问题"}, {"kind": "done"}]])
        audit = TailAudit()
        audit.start_turn(0)
        result = audit.run(client, self._messages(), [], SAMPLING, 30.0)
        self.assertNotIn("[尾部强制自审]", result["text"])


if __name__ == "__main__":
    unittest.main()
