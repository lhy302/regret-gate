"""模块级验收：`payload_builder`（构建规范 §14.1 / §9）。"""

from __future__ import annotations

import json
import unittest

from core.payload_builder import (
    PROMPT_APPENDIX_STRONG,
    PROMPT_APPENDIX_WEAK,
    SYSTEM_PROMPT_BASE,
    PayloadBuilder,
    build_system_prompt,
    extract_appendix,
)
from core.types import ToolResult


class FourIronRulesTest(unittest.TestCase):
    """§9.3 四条铁律逐个断言。"""

    def test_base_text_never_in_messages(self):
        builder = PayloadBuilder()
        finalized = "FINAL CONTENT"
        messages = builder.build(history=[], finalized_text=finalized)
        serialized = json.dumps(messages, ensure_ascii=False)
        self.assertIn(finalized, serialized)
        self.assertNotIn("BASE_TEXT_MARKER", serialized)

    def test_history_base_text_field_is_dropped(self):
        builder = PayloadBuilder()
        # 即使上层误传了 base_text，也不允许出现在 payload 里
        history = [
            {"role": "assistant", "content": "final", "base_text": "RAW_BASE_TEXT_SHOULD_NOT_APPEAR"},
        ]
        messages = builder.build(history=history)
        serialized = json.dumps(messages, ensure_ascii=False)
        self.assertNotIn("RAW_BASE_TEXT_SHOULD_NOT_APPEAR", serialized)
        self.assertEqual(set(messages[1].keys()), {"role", "content"})

    def test_revision_history_never_in_messages(self):
        builder = PayloadBuilder()
        history = [
            {"role": "assistant", "content": "final", "revisions": [{"id": "r1", "op": "replace"}]},
        ]
        messages = builder.build(history=history)
        self.assertNotIn("revisions", json.dumps(messages, ensure_ascii=False))

    def test_tail_audit_prompt_never_in_messages(self):
        builder = PayloadBuilder()
        history = [
            {"role": "user", "content": "[尾部强制自审]\n本回合输出已完成。", "is_tail_audit": True},
        ]
        messages = builder.build(history=history, finalized_text="done")
        serialized = json.dumps(messages, ensure_ascii=False)
        self.assertNotIn("[尾部强制自审]", serialized)

    def test_pending_internal_state_never_in_messages(self):
        builder = PayloadBuilder()
        history = [{"role": "assistant", "content": "x", "pending_actions": [{"id": "p1"}]}]
        messages = builder.build(history=history)
        self.assertNotIn("pending_actions", json.dumps(messages, ensure_ascii=False))

    def test_system_message_only_from_builder(self):
        builder = PayloadBuilder(system_prompt="SYS")
        messages = builder.build(history=[{"role": "system", "content": "INJECTED"}])
        self.assertEqual([m["content"] for m in messages if m["role"] == "system"], ["SYS"])
        self.assertNotIn("INJECTED", json.dumps(messages, ensure_ascii=False))


class StructureTest(unittest.TestCase):
    def test_messages_shape(self):
        builder = PayloadBuilder(system_prompt="SYS")
        result = ToolResult(tool_call_id="c1", tool_name="read_file", ok=True, output="data", executed=True)
        messages = builder.build(
            history=[{"role": "assistant", "content": "prev"}],
            user_input="task",
            finalized_text="final",
            tool_results=[result],
        )
        self.assertEqual(messages[0], {"role": "system", "content": "SYS"})
        self.assertEqual(messages[1], {"role": "assistant", "content": "prev"})
        self.assertEqual(messages[2], {"role": "user", "content": "task"})
        self.assertEqual(messages[3]["content"], "final")
        self.assertEqual(messages[4]["role"], "tool")
        self.assertEqual(messages[4]["tool_call_id"], "c1")
        self.assertEqual(messages[4]["content"], "data")

    def test_tool_calls_preserved(self):
        builder = PayloadBuilder()
        history = [{"role": "assistant", "content": "x", "tool_calls": [{"id": "c1"}]}]
        messages = builder.build(history=history)
        self.assertEqual(messages[1]["tool_calls"], [{"id": "c1"}])


class CacheBreakPointTest(unittest.TestCase):
    def test_unchanged_payload_break_point_is_at_end(self):
        builder = PayloadBuilder()
        builder.build(history=[], user_input="task")
        length = builder.last_payload_len
        builder.build(history=[], user_input="task")
        self.assertEqual(builder.last_break_point, length)

    def test_first_payload_break_point_is_zero(self):
        """首次请求没有“上一次 payload”可比较 → 破坏点记为 0。"""
        builder = PayloadBuilder()
        self.assertIsNone(builder.previous_payload)
        builder.compute_cache_break_point([{"role": "user", "content": "first"}])
        self.assertEqual(builder.last_break_point, 0)

    def test_break_point_points_at_earliest_difference(self):
        builder = PayloadBuilder()
        first = builder.build(history=[], user_input="AAAA")
        builder.build(history=[], user_input="AAAB")
        canonical_first = json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        canonical_second = json.dumps(
            [{"role": "system", "content": builder.system_prompt}, {"role": "user", "content": "AAAB"}],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        expected = next(
            (i for i, (a, b) in enumerate(zip(canonical_first, canonical_second)) if a != b),
            min(len(canonical_first), len(canonical_second)),
        )
        self.assertEqual(builder.last_break_point, expected)

    def test_break_ratio_bounded(self):
        builder = PayloadBuilder()
        builder.build(history=[], user_input="x")
        builder.build(history=[], user_input="y")
        self.assertGreaterEqual(builder.cache_break_ratio(), 0.0)
        self.assertLessEqual(builder.cache_break_ratio(), 1.0)

    def test_snapshot_contains_cache_fields(self):
        builder = PayloadBuilder()
        builder.build(history=[], user_input="x")
        snapshot = builder.snapshot()
        self.assertIn("cache_break_point", snapshot)
        self.assertIn("cache_break_ratio", snapshot)
        self.assertGreater(snapshot["payload_len"], 0)


class PromptConditionTest(unittest.TestCase):
    """§11.3：prompt_condition 只影响 system_prompt 的追加部分。"""

    def test_none_has_no_appendix(self):
        self.assertEqual(build_system_prompt("none"), SYSTEM_PROMPT_BASE)
        self.assertEqual(extract_appendix(build_system_prompt("none")), "")

    def test_weak_appendix_verbatim(self):
        self.assertEqual(extract_appendix(build_system_prompt("weak")), PROMPT_APPENDIX_WEAK)

    def test_strong_appendix_verbatim(self):
        self.assertEqual(extract_appendix(build_system_prompt("strong")), PROMPT_APPENDIX_STRONG)

    def test_base_part_identical_across_conditions(self):
        prompts = {c: build_system_prompt(c) for c in ("none", "weak", "strong")}
        for prompt in prompts.values():
            self.assertTrue(prompt.startswith(SYSTEM_PROMPT_BASE))
        self.assertTrue(prompts["weak"].startswith(prompts["none"]))
        self.assertTrue(prompts["strong"].startswith(prompts["none"]))

    def test_append_only_never_rewrites_base(self):
        for condition in ("weak", "strong"):
            prompt = build_system_prompt(condition)
            self.assertEqual(prompt[: len(SYSTEM_PROMPT_BASE)], SYSTEM_PROMPT_BASE)

    def test_unknown_condition_raises(self):
        with self.assertRaises(ValueError):
            build_system_prompt("bogus")


if __name__ == "__main__":
    unittest.main()
