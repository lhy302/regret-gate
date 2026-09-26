"""模块级验收：`runner` 配置加载（构建规范 §11）。

配置是实验设计的一部分，配置读错等于实验做错，因此这里对组矩阵、H 展开、
以及“唯一变量”约束做机制化断言。
"""

from __future__ import annotations

import unittest

from core.payload_builder import SYSTEM_PROMPT_BASE
from experiments.runner import (
    GROUP_ORDER,
    ConfigError,
    build_group_configs,
    collect_group_metrics,
    load_base_config,
)
from tasks.schema import category_counts

EXPECTED_MATRIX = {
    "A": (False, False, False, False, "none"),
    "B": (True, False, False, False, "none"),
    "C": (False, True, False, False, "none"),
    "D": (False, False, True, False, "none"),
    "E": (True, True, False, False, "none"),
    "F": (True, True, True, True, "none"),
    "G": (True, True, True, True, "strong"),
    "H": (False, False, False, True, "none"),
}


class BaseConfigTest(unittest.TestCase):
    def test_base_config_loads_required_sections(self):
        base = load_base_config()
        for key in ("provider", "sampling", "timeout", "enabled_mechanisms", "risk_policy",
                    "tasks", "runs_per_task"):
            self.assertIn(key, base)

    def test_sampling_is_fully_pinned(self):
        base = load_base_config()
        sampling = base["sampling"]
        for key in ("model", "temperature", "top_p", "max_tokens", "seed"):
            self.assertIn(key, sampling)

    def test_risk_policy_default_is_intercept(self):
        base = load_base_config()
        self.assertEqual(base["risk_policy"]["default_level"], "intercept")
        rule_ids = {rule.get("id") for rule in base["risk_policy"]["rules"]}
        for required in ("shell_read_allow", "shell_destructive_intercept", "read_file_allow",
                         "write_file_buffer", "draft_allow"):
            self.assertIn(required, rule_ids)

    def test_missing_base_config_raises(self):
        with self.assertRaises(ConfigError):
            load_base_config("no_such_dir")


class GroupMatrixTest(unittest.TestCase):
    def test_every_group_loads_with_expected_mechanisms(self):
        for group, (router, tail, auditor, stack, prompt) in EXPECTED_MATRIX.items():
            config = build_group_configs(group, task_limit=8)[0]
            mechanisms = config.enabled_mechanisms
            self.assertEqual(
                (mechanisms.risk_router, mechanisms.tail_audit,
                 mechanisms.external_auditor, mechanisms.revision_stack),
                (router, tail, auditor, stack),
                group,
            )
            self.assertEqual(config.prompt_condition, prompt, group)

    def test_group_order_cover_all(self):
        self.assertEqual(GROUP_ORDER, ["A", "B", "C", "D", "E", "F", "G", "H"])

    def test_unknown_group_raises(self):
        with self.assertRaises(ConfigError):
            build_group_configs("Z")

    def test_only_two_variables_differ(self):
        """铁律 8：组之间只允许 prompt_condition 与 enabled_mechanisms 不同。"""
        signatures = set()
        for group in "ABCDEFG":
            config = build_group_configs(group, task_limit=8)[0]
            signatures.add(
                (
                    config.sampling.model,
                    config.sampling.temperature,
                    config.sampling.top_p,
                    config.sampling.max_tokens,
                    config.sampling.seed,
                    config.timeout,
                    config.runs_per_task,
                    tuple(config.tasks),
                    getattr(config, "provider", "fake"),
                    getattr(config, "system_prompt_base", SYSTEM_PROMPT_BASE),
                )
            )
        self.assertEqual(len(signatures), 1, "除 prompt_condition 与 enabled_mechanisms 外必须完全一致")

    def test_h_expands_to_three_subgroups_sharing_everything_else(self):
        configs = build_group_configs("H", task_limit=12)
        self.assertEqual([c.sub_group for c in configs], ["H-none", "H-weak", "H-strong"])
        self.assertEqual([c.prompt_condition for c in configs], ["none", "weak", "strong"])
        self.assertEqual(len({tuple(c.tasks) for c in configs}), 1)
        self.assertEqual(len({c.sampling.to_dict().__repr__() for c in configs}), 1)
        self.assertEqual(len({c.enabled_mechanisms.to_dict().__repr__() for c in configs}), 1)
        for config in configs:
            self.assertEqual(config.group, "H")
            self.assertEqual(len(config.tasks), 12)
            self.assertEqual(sum(category_counts(getattr(config, "task_objects")).values()), 12)

    def test_limit_is_stratified(self):
        config = build_group_configs("F", task_limit=16)[0]
        self.assertEqual(
            category_counts(getattr(config, "task_objects")),
            {"long_code": 4, "long_text": 4, "short_command": 4, "mixed": 4},
        )

    def test_limit_can_be_overridden_per_call(self):
        config = build_group_configs("F", task_limit=4)[0]
        self.assertEqual(len(config.tasks), 4)

    def test_runs_override(self):
        config = build_group_configs("F", task_limit=4, runs_override=3)[0]
        self.assertEqual(config.runs_per_task, 3)


class RiskPolicyWiringTest(unittest.TestCase):
    """真实 YAML → 配置对象 → harness → 审计决策，不只检查字段存在。"""

    def _configs(self, policy, group="B"):
        import shutil
        import tempfile
        from pathlib import Path
        import yaml
        from experiments.runner import CONFIGS_DIR

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name) / "configs"
        shutil.copytree(CONFIGS_DIR, root)
        path = root / "base.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        raw["risk_policy"] = policy
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        return build_group_configs(group, configs_dir=str(root), task_limit=4)

    def _harness(self, config):
        from core.audit_logger import NullAuditLogger
        from experiments.harness import MechanismHarness
        from llm.fake_client import FakeClient

        target = next(t for t in config.task_objects if t.category == "short_command")
        client = FakeClient()
        client.set_task(target)
        logger = NullAuditLogger(run_id="policy", group=config.group, task_id=target.id)
        return MechanismHarness(config=config, task=target, client=client, audit_logger=logger)

    def test_yaml_rule_changes_actual_run_decisions(self):
        for level in ("allow", "intercept"):
            with self.subTest(level=level):
                policy = {"rules": [{"id": "configured_shell", "condition": {
                    "tool_name": "execute_shell"}, "level": level}]}
                harness = self._harness(self._configs(policy)[0])
                result = harness.run()
                self.assertIsNone(result.error)
                self.assertEqual(result.intercept_count > 0, level == "intercept")
                decisions = harness.audit.events("risk_decision")
                self.assertTrue(decisions)
                self.assertTrue(any("configured_shell" in str(event) for event in decisions))

    def test_explicit_empty_policy_does_not_restore_allow_rules(self):
        from core.types import ToolCall
        harness = self._harness(self._configs({})[0])
        decision = harness.risk_router.classify(ToolCall(id="read", name="read_file"))
        self.assertEqual(decision.level, "intercept")

    def test_policy_options_reach_router(self):
        from core.types import ToolCall
        policy = {"default_level": "buffer", "case_sensitive": True,
                  "split_compound_commands": False, "allowed_roots": ["./sandbox"],
                  "forbidden_roots": ["./private"], "rules": [{
                      "condition": {"tool_name": "execute_shell", "arg_pattern": "^LOOK"},
                      "level": "allow"}]}
        harness = self._harness(self._configs(policy)[0])
        router = harness.risk_router
        self.assertEqual(router.classify(ToolCall(id="x", name="execute_shell",
                         args={"command": "look"})).level, "buffer")
        self.assertEqual(router.classify(ToolCall(id="x", name="execute_shell",
                         args={"command": "LOOK; unknown"})).level, "allow")
        self.assertEqual(router.policy.allowed_roots, ["./sandbox"])
        self.assertEqual(router.policy.forbidden_roots, ["./private"])

    def test_policy_is_serialized_and_isolated_between_h_subgroups(self):
        policy = {"rules": [{"condition": {"tool_name": "read_file"}, "level": "buffer"}]}
        configs = self._configs(policy, group="H")
        self.assertTrue(all(c.risk_policy == policy for c in configs))
        snapshot = configs[0].to_dict()
        snapshot["risk_policy"]["rules"][0]["level"] = "allow"
        self.assertEqual(configs[0].risk_policy, policy)
        harness = self._harness(configs[0])
        configs[0].risk_policy["rules"][0]["level"] = "intercept"
        self.assertEqual(configs[1].risk_policy, policy)
        self.assertEqual(harness.risk_router.policy.rules[0]["level"], "buffer")

    def test_missing_policy_preserves_direct_caller_defaults(self):
        from core.types import ToolCall
        config = build_group_configs("B", task_limit=4)[0]
        config.risk_policy = None
        harness = self._harness(config)
        self.assertEqual(harness.risk_router.classify(
            ToolCall(id="read", name="read_file")).level, "allow")

    def test_all_groups_share_base_policy(self):
        expected = load_base_config()["risk_policy"]
        for group in GROUP_ORDER:
            for config in build_group_configs(group, task_limit=4):
                self.assertEqual(config.risk_policy, expected)


class MetricsCollectionTest(unittest.TestCase):
    def test_collect_group_metrics_on_missing_dir(self):
        self.assertEqual(collect_group_metrics("no_such_dir_for_metrics"), [])

    def test_collect_group_metrics_merges_sorted_files(self):
        import json
        import os
        import tempfile

        directory = tempfile.mkdtemp(prefix="harness_collect_")
        try:
            metrics_dir = os.path.join(directory, "metrics")
            os.makedirs(metrics_dir, exist_ok=True)
            for name, group in (("B.jsonl", "B"), ("A.jsonl", "A")):
                with open(os.path.join(metrics_dir, name), "w", encoding="utf-8") as handle:
                    handle.write(json.dumps({"group": group, "run_id": f"{group}-1"}) + "\n")
            rows = collect_group_metrics(directory)
            self.assertEqual([r["group"] for r in rows], ["A", "B"])
        finally:
            import shutil

            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
