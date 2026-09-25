"""模块级验收：`revision_stack`（构建规范 §14.1 / §6）。"""

from __future__ import annotations

import unittest

from core.revision_stack import RevisionStack, resolve_range
from core.types import (
    REV_APPLIED,
    REV_REJECTED,
    REV_STAGED,
    FinalizeResult,
    Revision,
)


def make_rev(stack: RevisionStack, target: str, op: str = "replace", payload: str = "X", created_at: int = 1):
    rev = Revision(id="", turn=stack.turn, created_at=created_at, target_text=target, op=op, payload=payload)
    return rev


class ResolveRangeTest(unittest.TestCase):
    def test_exact_match(self):
        text = "hello world"
        self.assertEqual(resolve_range(text, "world"), (6, 11))

    def test_leading_trailing_whitespace_match(self):
        text = "def f():\n    return 1\n"
        self.assertEqual(resolve_range(text, "  def f():  "), (0, 8))

    def test_whitespace_normalized_match(self):
        text = "alpha    beta\n\tgamma"
        start, end = resolve_range(text, "alpha beta gamma")
        self.assertEqual(text[start:end], "alpha    beta\n\tgamma")

    def test_failure_returns_minus_one(self):
        self.assertEqual(resolve_range("abc", "zzz"), (-1, -1))

    def test_empty_target_is_failure(self):
        self.assertEqual(resolve_range("abc", ""), (-1, -1))

    def test_no_fuzzy_matching(self):
        # 只差一个字符也必须失败（禁止模糊匹配，铁律 §6.3）
        self.assertEqual(resolve_range("return value_one", "return value_two"), (-1, -1))
        self.assertEqual(resolve_range("def f(a):", "def f(b):"), (-1, -1))


class PushRangeLockTest(unittest.TestCase):
    def setUp(self):
        self.stack = RevisionStack()
        self.stack.start_turn(turn=0, base_text="alpha beta gamma", start_step=5)

    def test_accepts_revision_in_range(self):
        rev = make_rev(self.stack, "beta", payload="BETA", created_at=6)
        self.assertTrue(self.stack.push(rev))
        self.assertEqual(rev.status, REV_STAGED)
        self.assertTrue(rev.id.startswith("r0_1"))

    def test_rejects_other_turn(self):
        rev = Revision(id="", turn=7, created_at=6, target_text="beta", op="replace", payload="X")
        self.assertFalse(self.stack.push(rev))
        self.assertEqual(rev.status, REV_REJECTED)
        self.assertIn("range lock", rev.rejected_reason)

    def test_rejects_created_at_before_turn_start(self):
        rev = make_rev(self.stack, "beta", created_at=1)
        self.assertFalse(self.stack.push(rev))
        self.assertEqual(rev.status, REV_REJECTED)

    def test_rejects_duplicate_id(self):
        first = Revision(id="call_1", turn=0, created_at=6, target_text="beta", op="replace", payload="B")
        self.assertTrue(self.stack.push(first))
        duplicate = Revision(id="call_1", turn=0, created_at=7, target_text="beta", op="replace", payload="B")
        self.assertFalse(self.stack.push(duplicate))
        self.assertEqual(duplicate.status, REV_REJECTED)
        self.assertIn("duplicate", duplicate.rejected_reason)

    def test_rejects_empty_target(self):
        rev = make_rev(self.stack, "")
        self.assertFalse(self.stack.push(rev))
        self.assertEqual(rev.status, REV_REJECTED)

    def test_push_never_raises(self):
        for bad in (
            Revision(id="", turn=0, created_at=6, target_text="zzz", op="replace", payload="X"),
            Revision(id="", turn=0, created_at=6, target_text="beta", op="bogus", payload="X"),
            Revision(id="", turn=0, created_at=6, target_text="beta", op="replace", payload=None),
        ):
            try:
                self.stack.push(bad)
            except Exception as exc:  # noqa: BLE001
                self.fail(f"push must not raise, got {exc!r}")


class FinalizeTest(unittest.TestCase):
    def _stack(self, base_text: str):
        stack = RevisionStack()
        stack.start_turn(turn=0, base_text=base_text, start_step=0)
        return stack

    def test_replace_applied(self):
        stack = self._stack("hello world")
        stack.push(Revision(id="", turn=0, created_at=1, target_text="world", op="replace", payload="there"))
        result = stack.finalize()
        self.assertIsInstance(result, FinalizeResult)
        self.assertEqual(result.finalized_text, "hello there")
        self.assertEqual(len(result.applied), 1)
        self.assertEqual(result.cache_break_point, 6)

    def test_erase_applied(self):
        stack = self._stack("keep DROP keep")
        stack.push(Revision(id="", turn=0, created_at=1, target_text="DROP ", op="erase", payload=""))
        self.assertEqual(stack.finalize().finalized_text, "keep keep")

    def test_insert_after_located_fragment(self):
        stack = self._stack("head tail")
        stack.push(Revision(id="", turn=0, created_at=1, target_text="head", op="insert", payload=" [NEW]"))
        self.assertEqual(stack.finalize().finalized_text, "head [NEW] tail")

    def test_whitespace_normalized_target_applies(self):
        stack = self._stack("alpha    beta")
        stack.push(Revision(id="", turn=0, created_at=1, target_text="alpha beta", op="replace", payload="A B"))
        self.assertEqual(stack.finalize().finalized_text, "A B")

    def test_failed_match_marked_rejected_not_fuzzy(self):
        stack = self._stack("some text")
        rev = Revision(id="", turn=0, created_at=1, target_text="missing fragment", op="replace", payload="X")
        stack.push(rev)
        result = stack.finalize()
        self.assertEqual(result.finalized_text, "some text")
        self.assertEqual(result.rejected, [rev])
        self.assertEqual(rev.status, REV_REJECTED)
        self.assertIsNone(rev.resolved_range)
        self.assertIn("fuzzy matching is forbidden", rev.rejected_reason)

    def test_applied_in_created_at_order_not_id_order(self):
        stack = self._stack("AAA BBB CCC")
        # id 顺序与 created_at 顺序相反：必须按 created_at 应用
        stack.push(Revision(id="zzz", turn=0, created_at=2, target_text="CCC", op="replace", payload="c"))
        stack.push(Revision(id="aaa", turn=0, created_at=1, target_text="AAA", op="replace", payload="a"))
        result = stack.finalize()
        self.assertEqual(result.finalized_text, "a BBB c")
        self.assertEqual([r.created_at for r in result.applied], [1, 2])

    def test_conflict_relocates_after_earlier_application(self):
        stack = self._stack("token = 1")
        stack.push(Revision(id="", turn=0, created_at=1, target_text="token = 1", op="replace", payload="token = 2"))
        stack.push(Revision(id="", turn=0, created_at=2, target_text="2", op="replace", payload="3"))
        result = stack.finalize()
        # 第二条基于第一条应用后的结果重新定位
        self.assertEqual(result.finalized_text, "token = 3")
        self.assertEqual(len(result.applied), 2)

    def test_no_rollback_of_applied_revisions(self):
        stack = self._stack("alpha beta")
        stack.push(Revision(id="", turn=0, created_at=1, target_text="alpha", op="replace", payload="ALPHA"))
        stack.push(Revision(id="", turn=0, created_at=2, target_text="nope", op="replace", payload="X"))
        result = stack.finalize()
        self.assertEqual(result.finalized_text, "ALPHA beta")
        self.assertEqual(len(result.applied), 1)
        self.assertEqual(len(result.rejected), 1)

    def test_cache_break_point_zero_when_first_char_changes(self):
        stack = self._stack("abc")
        stack.push(Revision(id="", turn=0, created_at=1, target_text="abc", op="replace", payload="zbc"))
        self.assertEqual(stack.finalize().cache_break_point, 0)

    def test_cache_break_point_at_end_when_identical(self):
        stack = self._stack("abc")
        result = stack.finalize()
        self.assertEqual(result.cache_break_point, len("abc"))

    def test_range_lock_blocks_out_of_bounds_replacement(self):
        stack = self._stack("short")
        rev = Revision(id="", turn=0, created_at=1, target_text="short", op="insert", payload="!!!")
        stack.push(rev)
        # insert 只在定位到的区间之后追加，越界不可能发生：断言区间始终落在 base_text 内
        stack.finalize()
        start, end = rev.resolved_range
        self.assertGreaterEqual(start, 0)
        self.assertLessEqual(end, len("short"))

    def test_snapshot_is_serializable_and_keeps_rejected(self):
        stack = self._stack("abc")
        stack.push(Revision(id="", turn=0, created_at=1, target_text="zzz", op="replace", payload="X"))
        stack.finalize()
        snapshot = stack.snapshot()
        self.assertEqual(snapshot["base_text"], "abc")
        self.assertEqual(len(snapshot["revisions"]), 1)
        self.assertEqual(snapshot["revisions"][0]["status"], REV_REJECTED)
        self.assertTrue(snapshot["lock_violations"])

    def test_preview_text_does_not_change_status(self):
        stack = self._stack("abc")
        rev = Revision(id="", turn=0, created_at=1, target_text="abc", op="replace", payload="xyz")
        stack.push(rev)
        self.assertEqual(stack.preview_text(), "xyz")
        self.assertEqual(rev.status, REV_STAGED)

    def test_applied_revision_status_and_range_recorded(self):
        stack = self._stack("abcdef")
        rev = Revision(id="", turn=0, created_at=1, target_text="cd", op="replace", payload="CD")
        stack.push(rev)
        stack.finalize()
        self.assertEqual(rev.status, REV_APPLIED)
        self.assertEqual(rev.resolved_range, (2, 4))


if __name__ == "__main__":
    unittest.main()
