"""模块级验收：`risk_router`（构建规范 §14.1 / §4）。"""

from __future__ import annotations

import unittest

from core.risk_router import RiskPolicy, RiskRouter
from core.tool_call_parser import default_registry
from core.types import RISK_ALLOW, RISK_BUFFER, RISK_INTERCEPT, ToolCall


def call(name: str, **args) -> ToolCall:
    return ToolCall(id="c1", name=name, args=args, raw_args="")


class MinimalRuleSetTest(unittest.TestCase):
    """§4.3 最小规则集逐条验收。"""

    def setUp(self):
        self.router = RiskRouter(registry=default_registry())

    def test_get_content_allowed(self):
        decision = self.router.classify(call("execute_shell", command="Get-Content logs/app.log"))
        self.assertEqual(decision.level, RISK_ALLOW)
        self.assertIn("read-only shell command", decision.reason)
        self.assertIsNotNone(decision.matched_rule)

    def test_remove_item_intercepted(self):
        decision = self.router.classify(call("execute_shell", command="Remove-Item -Recurse -Force temp"))
        self.assertEqual(decision.level, RISK_INTERCEPT)
        self.assertIn("destructive", decision.reason)

    def test_git_status_allowed_git_push_force_intercepted(self):
        self.assertEqual(self.router.classify(call("execute_shell", command="git status")).level, RISK_ALLOW)
        self.assertEqual(self.router.classify(call("execute_shell", command="git diff HEAD~1")).level, RISK_ALLOW)
        self.assertEqual(
            self.router.classify(call("execute_shell", command="git push --force origin main")).level,
            RISK_INTERCEPT,
        )

    def test_read_file_allow_write_file_buffer(self):
        self.assertEqual(self.router.classify(call("read_file", path="a.txt")).level, RISK_ALLOW)
        self.assertEqual(
            self.router.classify(call("write_file", path="a.txt", content="x")).level, RISK_BUFFER
        )

    def test_http_method_grading(self):
        self.assertEqual(self.router.classify(call("http_request", url="u", method="GET")).level, RISK_ALLOW)
        for method in ("POST", "PUT", "DELETE"):
            self.assertEqual(
                self.router.classify(call("http_request", url="u", method=method)).level, RISK_BUFFER
            )

    def test_draft_commit_revision_allowed(self):
        decision = self.router.classify(
            call("draft.commit_revision", target_text="a", op="replace", payload="b")
        )
        self.assertEqual(decision.level, RISK_ALLOW)

    def test_unknown_shell_command_buffers(self):
        self.assertEqual(self.router.classify(call("execute_shell", command="python build.py")).level, RISK_BUFFER)

    def test_unknown_tool_hits_default_intercept(self):
        decision = self.router.classify(call("some_new_tool", x=1))
        self.assertEqual(decision.level, RISK_INTERCEPT)
        self.assertIn("no rule matched", decision.reason)

    def test_malformed_tool_call_intercepted(self):
        bad = ToolCall(id="c", name="execute_shell", args={}, raw_args="{", malformed=True,
                       malformed_reason="invalid json")
        decision = self.router.classify(bad)
        self.assertEqual(decision.level, RISK_INTERCEPT)
        self.assertEqual(decision.matched_rule, "malformed_arguments")


class ParameterAwareTest(unittest.TestCase):
    """必须解析参数，不能只看工具名（§15.2）。"""

    def setUp(self):
        self.router = RiskRouter(registry=default_registry())

    def test_same_tool_different_parameters(self):
        same_tool = "execute_shell"
        self.assertEqual(self.router.classify(call(same_tool, command="Get-Content x")).level, RISK_ALLOW)
        self.assertEqual(self.router.classify(call(same_tool, command="Remove-Item x")).level, RISK_INTERCEPT)

    def test_shell_only_by_tool_name_would_be_wrong(self):
        # 只按工具名的实现会把两种调用归为同级；这里断言它们确实不同
        allow = self.router.classify(call("execute_shell", command="ls -la"))
        intercept = self.router.classify(call("execute_shell", command="rm -rf /tmp/x"))
        self.assertNotEqual(allow.level, intercept.level)


class CompoundCommandTest(unittest.TestCase):
    def setUp(self):
        self.router = RiskRouter(registry=default_registry())

    def test_and_operator_takes_strictest(self):
        decision = self.router.classify(call("execute_shell", command="Get-Content a.log && Remove-Item b.log"))
        self.assertEqual(decision.level, RISK_INTERCEPT)

    def test_semicolon_takes_strictest(self):
        decision = self.router.classify(call("execute_shell", command="ls; rm -rf build"))
        self.assertEqual(decision.level, RISK_INTERCEPT)

    def test_pipe_takes_strictest(self):
        decision = self.router.classify(call("execute_shell", command="cat log | Remove-Item x"))
        self.assertEqual(decision.level, RISK_INTERCEPT)

    def test_all_readonly_compound_stays_allow(self):
        decision = self.router.classify(call("execute_shell", command="Get-Content a; git status"))
        self.assertEqual(decision.level, RISK_ALLOW)

    def test_compound_recorded_segment_in_matched_rule(self):
        decision = self.router.classify(call("execute_shell", command="ls && rm -rf x"))
        self.assertIn("rm -rf x", decision.matched_rule or "")


class PathScopeTest(unittest.TestCase):
    def setUp(self):
        self.router = RiskRouter(registry=default_registry())

    def test_traversal_intercepted(self):
        decision = self.router.classify(call("execute_shell", command="Remove-Item ../../etc/passwd"))
        self.assertEqual(decision.level, RISK_INTERCEPT)

    def test_traversal_intercepted_even_for_readonly_command(self):
        decision = self.router.classify(call("execute_shell", command="Get-Content ../../etc/passwd"))
        self.assertEqual(decision.level, RISK_INTERCEPT)
        self.assertEqual(decision.matched_rule, "path_scope")

    def test_forbidden_root_intercepted(self):
        decision = self.router.classify(call("execute_shell", command="cat /etc/shadow"))
        self.assertEqual(decision.level, RISK_INTERCEPT)

    def test_relative_path_inside_allowed_root_allowed(self):
        decision = self.router.classify(call("execute_shell", command="Get-Content logs/app.log"))
        self.assertEqual(decision.level, RISK_ALLOW)


class NonStringArgumentTest(unittest.TestCase):
    def test_non_string_command_falls_back_to_default(self):
        router = RiskRouter(registry=default_registry())
        decision = router.classify(call("execute_shell", command=["Get-Content", "x"]))
        self.assertEqual(decision.level, RISK_INTERCEPT)
        self.assertEqual(decision.matched_rule, "non_string_arg")

    def test_arg_pattern_ignored_for_non_string_field(self):
        policy = RiskPolicy(
            rules=[{"condition": {"tool_name": "t", "arg_field": "value", "arg_pattern": "^a"}, "level": "allow",
                    "reason": "r"}],
            default_level=RISK_BUFFER,
        )
        router = RiskRouter(policy=policy, registry=default_registry())
        self.assertEqual(router.classify(call("t", value="abc")).level, RISK_ALLOW)
        self.assertEqual(router.classify(call("t", value=123)).level, RISK_BUFFER)
        self.assertEqual(router.classify(call("t", value=["abc"])).level, RISK_BUFFER)


class CaseSensitivityTest(unittest.TestCase):
    def test_case_insensitive_by_default(self):
        router = RiskRouter(registry=default_registry())
        self.assertEqual(router.classify(call("execute_shell", command="REMOVE-ITEM x")).level, RISK_INTERCEPT)
        self.assertEqual(router.classify(call("execute_shell", command="remove-item x")).level, RISK_INTERCEPT)

    def test_case_sensitive_when_configured(self):
        policy = RiskPolicy(
            rules=[{"condition": {"tool_name": "execute_shell", "arg_pattern": "^rm "}, "level": "intercept",
                    "reason": "r"}],
            default_level=RISK_BUFFER,
            case_sensitive=True,
        )
        router = RiskRouter(policy=policy, registry=default_registry())
        self.assertEqual(router.classify(call("execute_shell", command="rm -rf x")).level, RISK_INTERCEPT)
        self.assertEqual(router.classify(call("execute_shell", command="RM -rf x")).level, RISK_BUFFER)


class FalseInterceptTest(unittest.TestCase):
    def setUp(self):
        self.router = RiskRouter(registry=default_registry())

    def test_true_positive_not_counted_as_false_intercept(self):
        tool_call = call("execute_shell", command="Remove-Item -Recurse -Force cache/a.log")
        self.assertFalse(self.router.is_false_intercept(tool_call, ["Remove-Item"]))

    def test_benign_operation_intercepted_is_false_intercept(self):
        # 只读命令 + 路径越权：风险规则命中，但它不是任务标注的高危操作 → 计为误拦截
        tool_call = call("execute_shell", command="Get-Content ../../etc/passwd")
        self.assertTrue(self.router.is_false_intercept(tool_call, ["Remove-Item"]))

    def test_benign_command_hitting_path_rule_is_false_intercept(self):
        tool_call = call("execute_shell", command="Get-Content ../../etc/passwd")
        decision = self.router.classify(tool_call)
        self.assertEqual(decision.level, RISK_INTERCEPT)
        self.assertIn("path traversal", decision.reason)
        self.assertTrue(self.router.is_false_intercept(tool_call, ["Remove-Item", "git reset --hard"]))

    def test_compound_command_with_declared_dangerous_part_is_not_false_intercept(self):
        tool_call = call("execute_shell", command="ls cache && Remove-Item cache/a.log")
        self.assertFalse(self.router.is_false_intercept(tool_call, ["Remove-Item"]))

    def test_multi_word_operation_matches_by_prefix(self):
        # `git reset --hard` 的危险面是整条指令，不是 `git` 这个可执行名
        tool_call = call("execute_shell", command="git reset --hard HEAD~1")
        self.assertFalse(self.router.is_false_intercept(tool_call, ["git reset --hard"]))
        self.assertFalse(self.router.is_false_intercept(tool_call, ["git push --force", "git reset --hard"]))

    def test_benign_git_command_is_false_intercept(self):
        # 复合命令里的 Remove-Item 未被任务标注 → 该拦截是误拦截
        tool_call = call("execute_shell", command="git stash list && Remove-Item x")
        self.assertTrue(self.router.is_false_intercept(tool_call, ["git reset --hard"]))
        # 同一个调用，若任务声明了 Remove-Item，则不算误拦截
        self.assertFalse(self.router.is_false_intercept(tool_call, ["Remove-Item"]))

    def test_matched_rule_always_tagged(self):
        decision = self.router.classify(call("execute_shell", command="Remove-Item x"))
        self.assertIsNotNone(decision.matched_rule)
        self.assertIsNotNone(decision.rule_id)


class PolicyFromConfigTest(unittest.TestCase):
    def test_from_config_roundtrip(self):
        cfg = {
            "default_level": "buffer",
            "case_sensitive": True,
            "rules": [
                {"id": "r1", "condition": {"tool_name": "x"}, "level": "allow", "reason": "ok"},
            ],
        }
        router = RiskRouter.from_config({"risk_policy": cfg})
        decision = router.classify(call("x"))
        self.assertEqual(decision.level, RISK_ALLOW)
        self.assertEqual(decision.rule_id, "r1")
        self.assertEqual(router.classify(call("y")).level, RISK_BUFFER)


if __name__ == "__main__":
    unittest.main()
