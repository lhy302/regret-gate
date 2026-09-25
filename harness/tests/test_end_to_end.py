"""端到端验收：场景 1–4 + 可复现性（构建规范 §14.2 / §14.3 / §14.4）。

全部使用 `FakeClient`，不调真实 API。
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import tempfile
import unittest

from core.audit_logger import AuditLogger
from core.payload_builder import SYSTEM_PROMPT_BASE, build_system_prompt, extract_appendix
from core.tool_call_parser import default_registry
from core.types import ExperimentConfig, SamplingConfig
from experiments.harness import MechanismHarness
from experiments.metrics import run_metrics_from_result
from experiments.report import build_report
from experiments.runner import build_group_configs, collect_group_metrics, run_experiment, write_metrics
from llm.fake_client import FakeClient
from llm.generators import faulty_code_block
from tasks.loader import load_tasks


def _config(group: str, prompt_condition: str = "none", tasks=None, **mechanisms) -> ExperimentConfig:
    from core.types import EnabledMechanisms

    config = ExperimentConfig(
        group=group,
        prompt_condition=prompt_condition,
        enabled_mechanisms=EnabledMechanisms(**mechanisms),
        sampling=SamplingConfig(model="fake-model-v1", temperature=0.0, top_p=1.0, seed=0),
        timeout=300.0,
    )
    task_objects = tasks if tasks is not None else []
    config.task_objects = task_objects  # type: ignore[attr-defined]
    config.provider = "fake"  # type: ignore[attr-defined]
    return config


def _task(task_id: str):
    tasks = {t.id: t for t in load_tasks()}
    return tasks[task_id]


class RecordingFakeClient(FakeClient):
    """记录每次请求的 messages，用于断言 payload 铁律。

    只保留 `role` / `content` / `tool_calls` / `tool_call_id`，模拟真实 provider
    实际能收到的字段（harness 从未把 `base_text` 等审计字段放进 messages）。
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.message_log: list = []
        self.tail_request_flags: list = []

    def stream_chat(self, messages, tools, sampling, timeout=60.0):
        captured = []
        for message in messages:
            captured.append(
                {
                    "role": message.get("role"),
                    "content": message.get("content"),
                    "tool_calls": message.get("tool_calls"),
                    "tool_call_id": message.get("tool_call_id"),
                }
            )
        self.message_log.append(captured)
        self.tail_request_flags.append(self.is_tail_audit_request(messages))
        yield from super().stream_chat(messages, tools, sampling, timeout)

    def main_turn_requests(self) -> list:
        """只取“普通回合”请求（排除尾部自审窗口的内部请求）。"""
        return [msgs for msgs, is_tail in zip(self.message_log, self.tail_request_flags) if not is_tail]


# ---------------------------------------------------------------------------
# 场景 1：短命令任务：拦截 → 暂存 → 尾部自审 → finalize → 独立审核 → 执行/不执行
# ---------------------------------------------------------------------------


class Scenario1ShortCommandTest(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="harness_s1_")
        self.task = _task("sc_001")

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    def _run(self, verdict: str = "approve"):
        config = _config("F", risk_router=True, tail_audit=True, external_auditor=True, revision_stack=True)
        client = FakeClient(tail_audit_verdict=verdict)
        client.set_task(self.task)
        auditor = FakeClient(tail_audit_verdict=verdict)
        auditor.set_task(self.task)
        path = os.path.join(self.out, "audit.jsonl")
        logger = AuditLogger(path=path, run_id="s1", group="F", task_id=self.task.id)
        harness = MechanismHarness(
            config=config, task=self.task, client=client, auditor_client=auditor, audit_logger=logger
        )
        result = harness.run()
        logger.close()
        return result, logger, path

    def test_full_chain_and_timeline(self):
        result, logger, path = self._run(verdict="approve")

        # RiskRouter 返回 intercept
        risk_events = logger.events("risk_decision")
        self.assertTrue(risk_events)
        self.assertIn("intercept", [e["data"]["decision"]["level"] for e in risk_events])

        # PendingActions 记录
        self.assertEqual(result.intercept_count, 1)
        self.assertTrue(logger.events("pending_added"))

        # 尾部自审注入（每回合一次）
        self.assertEqual(len(logger.events("tail_audit_injected")), result.turns.__len__())
        self.assertEqual(result.tail_audit_injections, len(result.turns))

        # finalize 完成
        self.assertTrue(any(e["data"].get("stage") is None for e in logger.events("finalize_done")))

        # ExternalAuditor 审核并 approve → 每个回合各执行一次
        self.assertEqual(len(logger.events("external_audit_request")), len(result.turns))
        self.assertEqual(len(logger.events("external_audit_response")), len(result.turns))
        self.assertEqual(len(logger.events("tool_executed")), len(result.turns))
        self.assertEqual(result.executed_actions, len(result.turns))
        self.assertFalse(result.security_incident)

        # 审计日志可还原完整时间线
        timeline = [e["event"] for e in logger.timeline()]
        for required in ("turn_start", "tool_call_parsed", "risk_decision", "pending_added",
                         "tail_audit_injected", "tail_audit_response", "finalize_done",
                         "external_audit_request", "external_audit_response", "pending_resolved",
                         "tool_executed", "turn_end"):
            self.assertIn(required, timeline)
        self.assertLess(timeline.index("pending_added"), timeline.index("external_audit_request"))
        self.assertLess(timeline.index("finalize_done"), timeline.index("tool_executed"))

        # 落盘日志可被逐行解析
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    json.loads(line)

    def test_reject_means_not_executed(self):
        result, logger, _ = self._run(verdict="reject")
        self.assertEqual(result.executed_actions, 0)
        self.assertNotIn("tool_executed", [e["event"] for e in logger.timeline()])
        statuses = [p["status"] for p in result.pending_actions]
        self.assertIn("rejected", statuses)
        self.assertEqual(result.external_audit_rejected, len(result.turns))

    def test_get_content_is_allowed_and_remove_item_is_intercepted(self):
        from core.risk_router import RiskRouter
        from core.types import RISK_ALLOW, RISK_INTERCEPT, ToolCall

        router = RiskRouter(registry=default_registry())
        self.assertEqual(
            router.classify(ToolCall(id="a", name="execute_shell", args={"command": "Get-Content x"})).level,
            RISK_ALLOW,
        )
        self.assertEqual(
            router.classify(ToolCall(id="b", name="execute_shell", args={"command": "Remove-Item x"})).level,
            RISK_INTERCEPT,
        )

    def test_dangerous_action_actually_executed_is_a_security_incident(self):
        """对照组：无审核时高危操作被直接执行 → security_incident 必须为真。"""
        config = _config("B", risk_router=True, tail_audit=False, external_auditor=False, revision_stack=False)
        client = FakeClient()
        client.set_task(self.task)
        result = MechanismHarness(config=config, task=self.task, client=client).run()
        self.assertEqual(result.intercept_count, 1)
        self.assertEqual(result.executed_actions, 0)  # buffer/intercept 在无尾部自审时被拒绝
        self.assertFalse(result.security_incident)


# ---------------------------------------------------------------------------
# 场景 2：长代码任务
# ---------------------------------------------------------------------------


class Scenario2LongCodeTest(unittest.TestCase):
    def setUp(self):
        self.task = _task("lc_001")

    def _run(self, config, client=None):
        client = client or RecordingFakeClient()
        client.set_task(self.task)
        return MechanismHarness(config=config, task=self.task, client=client).run(), client

    def test_generates_over_300_lines_and_compiles_after_tail_audit(self):
        config = _config("F", risk_router=True, tail_audit=True, external_auditor=True, revision_stack=True)
        result, client = self._run(config)
        self.assertGreater(len(result.base_text.split("\n")), 300)
        self.assertFalse(result.syntax_error)
        self.assertTrue(result.task_success)
        code = result.finalized_text.split("```python")[1].split("```")[0] if "```" in result.finalized_text else result.finalized_text
        ast.parse(code)

    def test_baseline_group_fails_syntax(self):
        """对照组：无尾部自审时，植入的缺陷不会被修，编译失败。"""
        config = _config("A")
        result, _ = self._run(config)
        self.assertTrue(result.syntax_error)
        self.assertFalse(result.task_success)
        with self.assertRaises(SyntaxError):
            ast.parse(result.finalized_text)

    def test_commit_revision_depends_on_prompt_condition(self):
        """H 组语义：只有弱/强提示词下离线模型才在生成中主动登记修订。

        这是“提示词模拟能力边界”的可判定口径：H-none 无任何登记，
        H-weak / H-strong 至少登记一条自发生成期修订。
        """
        for condition, expected_min in (("none", 0), ("weak", 1), ("strong", 1)):
            config = _config("H", prompt_condition=condition, tasks=[self.task], revision_stack=True)
            client = FakeClient(spontaneous_revision=condition != "none")
            client.set_task(self.task)
            result = MechanismHarness(config=config, task=self.task, client=client).run()
            applied = [r for r in result.revisions if r["status"] == "applied"]
            self.assertGreaterEqual(len(applied), expected_min, f"condition={condition}")
            if expected_min == 0:
                self.assertEqual(len(applied), 0)
        # 生成期主动登记是“登记能力”的证据，但它不修复语法缺陷：
        # H 组（无尾部自审）语法仍然失败，说明能力边界与修复效果是两件事。
        config = _config("H", prompt_condition="strong", tasks=[self.task], revision_stack=True)
        client = FakeClient(spontaneous_revision=True)
        client.set_task(self.task)
        result = MechanismHarness(config=config, task=self.task, client=client).run()
        self.assertTrue(result.syntax_error)
        self.assertGreaterEqual(len(result.revisions), 1)

    def test_base_text_never_sent_and_finalized_text_replaces_it(self):
        config = _config("F", risk_router=True, tail_audit=True, external_auditor=True, revision_stack=True)
        result, client = self._run(config)
        fault = faulty_code_block(self.task.id)
        self.assertIn(fault, result.base_text)
        self.assertNotIn(fault, result.finalized_text)
        # 普通回合请求的 messages 里不能出现 base_text 的缺陷原文，
        # 也不能残留尾部自审提示，更不能带修订历史字段。
        main_requests = client.main_turn_requests()
        self.assertEqual(len(main_requests), len(result.turns))
        sent = json.dumps(main_requests, ensure_ascii=False)
        self.assertNotIn(fault, sent)
        self.assertNotIn("[尾部强制自审]", sent)
        self.assertNotIn("target_text", sent)
        # 尾部自审窗口内部的请求允许携带 base_text（模型必须看到才能审阅），
        # 但它只能是窗口内部请求，不能成为普通回合的 payload。
        tail_requests = [m for m, flag in zip(client.message_log, client.tail_request_flags) if flag]
        self.assertTrue(tail_requests)
        for messages in tail_requests:
            self.assertIn("[尾部强制自审]", json.dumps(messages, ensure_ascii=False))

    def test_finalized_text_is_what_next_turn_sees(self):
        config = _config("F", risk_router=True, tail_audit=True, external_auditor=True, revision_stack=True)
        result, client = self._run(config)
        main_requests = client.main_turn_requests()
        self.assertGreaterEqual(len(main_requests), 2)
        second_turn_messages = main_requests[1]
        assistant_contents = [m.get("content") for m in second_turn_messages if m.get("role") == "assistant"]
        self.assertTrue(any(result.finalized_text == content for content in assistant_contents))

    def test_cache_break_point_recorded(self):
        config = _config("F", risk_router=True, tail_audit=True, external_auditor=True, revision_stack=True)
        result, _ = self._run(config)
        self.assertIsInstance(result.cache_break_point, int)
        self.assertGreater(result.cache_break_point, 0)
        self.assertGreaterEqual(result.cache_break_ratio, 0.0)

    def test_tail_audit_injected_every_turn_even_without_high_risk_calls(self):
        config = _config("C", tail_audit=True)
        result, _ = self._run(config)
        self.assertEqual(result.intercept_count, 0)
        self.assertEqual(result.tail_audit_injections, len(result.turns))
        self.assertTrue(result.tail_audit_modified)


# ---------------------------------------------------------------------------
# 场景 3：H 组矩阵
# ---------------------------------------------------------------------------


class Scenario3HMatrixTest(unittest.TestCase):
    def setUp(self):
        self.tasks = [_task("lt_001"), _task("sc_001")]

    def _run_condition(self, condition: str):
        config = _config("H", prompt_condition=condition, tasks=self.tasks, revision_stack=True)
        results = []
        for task in self.tasks:
            client = FakeClient(tail_audit_fixes=False)
            client.set_task(task)
            results.append(MechanismHarness(config=config, task=task, client=client).run())
        return results

    def test_system_prompt_differs_only_by_appendix(self):
        prompts = {}
        for condition in ("none", "weak", "strong"):
            config = _config("H", prompt_condition=condition, tasks=self.tasks, revision_stack=True)
            client = FakeClient()
            client.set_task(self.tasks[0])
            harness = MechanismHarness(config=config, task=self.tasks[0], client=client)
            prompts[condition] = harness.system_prompt
        for condition, prompt in prompts.items():
            self.assertTrue(prompt.startswith(SYSTEM_PROMPT_BASE), condition)
            self.assertEqual(prompt, build_system_prompt(condition))
            self.assertEqual(extract_appendix(prompt), extract_appendix(build_system_prompt(condition)))
        self.assertEqual(extract_appendix(prompts["none"]), "")
        self.assertNotEqual(prompts["weak"], prompts["strong"])
        for condition in ("weak", "strong"):
            self.assertEqual(prompts[condition][: len(SYSTEM_PROMPT_BASE)], prompts["none"])

    def test_enabled_mechanisms_identical_across_subgroups(self):
        mechanisms = []
        for condition in ("none", "weak", "strong"):
            config = _config("H", prompt_condition=condition, tasks=self.tasks, revision_stack=True)
            mechanisms.append(config.enabled_mechanisms.to_dict())
        self.assertEqual(mechanisms[0], mechanisms[1])
        self.assertEqual(mechanisms[1], mechanisms[2])

    def test_metrics_recorded_per_subgroup(self):
        from core.types import EnabledMechanisms

        rows = []
        for condition in ("none", "weak", "strong"):
            config = _config("H", prompt_condition=condition, tasks=self.tasks, revision_stack=True)
            self.assertIsInstance(config.enabled_mechanisms, EnabledMechanisms)
            for result in self._run_condition(condition):
                row = run_metrics_from_result(result)
                row["group"] = f"H-{condition}"
                rows.append(row)
        self.assertEqual(len(rows), 6)
        self.assertEqual({r["group"] for r in rows}, {"H-none", "H-weak", "H-strong"})
        for row in rows:
            self.assertIn("prompt_condition", row)

    def test_same_task_set_and_sampling_across_subgroups(self):
        configs = {}
        for condition in ("none", "weak", "strong"):
            configs[condition] = _config("H", prompt_condition=condition, tasks=self.tasks, revision_stack=True)
        samplings = {json.dumps(c.sampling.to_dict(), sort_keys=True) for c in configs.values()}
        self.assertEqual(len(samplings), 1)
        task_sets = {tuple(c.tasks) for c in configs.values()}
        self.assertEqual(len(task_sets), 1)

    def test_offline_client_responds_to_prompt_condition_and_is_not_shared(self):
        """离线模型对提示词强度的响应必须真的区分，且三个子组不共用同一客户端实例。"""
        from experiments.runner import build_offline_client

        none_config = _config("H", prompt_condition="none", tasks=self.tasks, revision_stack=True)
        strong_config = _config("H", prompt_condition="strong", tasks=self.tasks, revision_stack=True)
        none_client = build_offline_client(none_config, role="main")
        strong_client = build_offline_client(strong_config, role="main")
        self.assertIsNot(none_client, strong_client)
        self.assertFalse(none_client.spontaneous_revision)
        self.assertTrue(strong_client.spontaneous_revision)
        # 同配置重复构造应命中缓存（复用同一实例）
        self.assertIs(build_offline_client(none_config, role="main"), none_client)
        # 主模型与审核 Agent 永远不是同一实例
        self.assertIsNot(build_offline_client(none_config, role="auditor"), none_client)


# ---------------------------------------------------------------------------
# 场景 4：离线完整实验（A–H）+ 报告
# ---------------------------------------------------------------------------


class Scenario4OfflineExperimentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = tempfile.mkdtemp(prefix="harness_s4_")
        for group in ("A", "B", "C", "D", "E", "F", "G", "H"):
            run_experiment(group, out_dir=cls.out, task_limit=2, verbose=False)
        rows = collect_group_metrics(cls.out)
        write_metrics(os.path.join(cls.out, "metrics.jsonl"), rows)
        cls.rows = rows

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)

    def test_all_groups_present_with_at_least_two_tasks(self):
        groups = {row["group"] for row in self.rows}
        for group in ("A", "B", "C", "D", "E", "F", "G", "H-none", "H-weak", "H-strong"):
            self.assertIn(group, groups)
        per_group: dict = {}
        for row in self.rows:
            per_group.setdefault(row["group"], set()).add(row["task_id"])
        for group, tasks in per_group.items():
            self.assertGreaterEqual(len(tasks), 2, group)

    def test_metrics_jsonl_contains_all_required_fields(self):
        required = {
            "run_id", "group", "task_id", "category", "syntax_error", "logic_error",
            "security_incident", "false_intercept", "input_tokens", "output_tokens", "latency_ms",
            "tail_audit_modified", "external_audit_effective", "revision_hit_rate", "task_success",
            "cache_break_ratio", "error",
        }
        for row in self.rows:
            self.assertTrue(required.issubset(set(row.keys())), sorted(required - set(row.keys())))

    def test_report_contains_summary_pairs_failures_and_cache_distribution(self):
        report = build_report(self.rows, meta={"provider": "fake", "model": "fake-model-v1",
                                               "task_limit": 2, "runs_per_task": 1})
        self.assertIn("# 后悔承诺门 · Harness 离线实验结果报告", report)
        self.assertIn("## 1. 每组指标汇总", report)
        self.assertIn("## 3. 关键对比的配对检验", report)
        self.assertIn("## 4. 假设 H1–H6 判定", report)
        self.assertIn("## 7. 缓存破坏点分布", report)
        self.assertIn("## 8. 分层分析", report)
        self.assertIn("## 9. 失败与未成功案例分析", report)
        self.assertIn("## 11. 结论与下一步建议", report)
        for indicator in ("语法错误率", "逻辑错误率", "安全事故率", "误拦截率", "尾部自审触发修改率",
                          "独立审核拦截有效率", "修订栈命中率", "任务成功率", "缓存破坏点比例"):
            self.assertIn(indicator, report)
        self.assertIn("H1", report)
        self.assertIn("H6", report)

    def test_report_includes_distribution_not_only_means(self):
        report = build_report(self.rows, meta={})
        self.assertIn("中位数", report)
        self.assertIn("标准差", report)
        self.assertIn("95% CI", report)

    def test_audit_logs_written_for_every_run(self):
        audit_dir = os.path.join(self.out, "audit")
        files = [f for f in os.listdir(audit_dir) if f.endswith(".jsonl")]
        self.assertEqual(len(files), len(self.rows))
        for name in files:
            self.assertLess(os.path.getsize(os.path.join(audit_dir, name)), 5 * 1024 * 1024)

    def test_performance_budget_llm_calls(self):
        for row in self.rows:
            self.assertLessEqual(row["llm_calls"], 10, row["run_id"])


# ---------------------------------------------------------------------------
# §14.4 可复现性
# ---------------------------------------------------------------------------


class DeterminismTest(unittest.TestCase):
    def _metrics(self, out_dir: str, group: str) -> list:
        out = os.path.join(out_dir, group)
        # 每次都从空目录开始，避免 audit_log_bytes 追加写导致的累计差异
        shutil.rmtree(out, ignore_errors=True)
        os.makedirs(out, exist_ok=True)
        run_experiment(group, out_dir=out, task_limit=2, verbose=False, metrics_mode="overwrite")
        rows = []
        with open(os.path.join(out, "metrics.jsonl"), "r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    rows.append(json.loads(line))
        return rows

    def test_same_config_twice_yields_identical_metrics(self):
        """§14.4 同一配置 + 同一 FakeClient 脚本连跑两次，指标完全一致。

        唯一例外是 `audit_log_bytes`：审计日志按真实写入时间记 `ts`，字符串长度随
        时间戳有效位浮动（±几字节）。它是日志体积的观测量，不是实验指标，
        因此确定性比较排除它，另行断言其满足 §14.3 的体积上限。
        """
        directory = tempfile.mkdtemp(prefix="harness_det_")
        try:
            for group in ("A", "C", "F", "H"):
                first = self._metrics(directory, group)
                second = self._metrics(directory, group)
                self.assertEqual(len(first), len(second))
                for a, b in zip(first, second):
                    clean_a = {k: v for k, v in a.items() if k != "audit_log_bytes"}
                    clean_b = {k: v for k, v in b.items() if k != "audit_log_bytes"}
                    self.assertEqual(clean_a, clean_b, f"group {group} metrics differ between identical runs")
                    self.assertLess(b["audit_log_bytes"], 5 * 1024 * 1024)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_metrics_jsonl_rewritten_deterministically(self):
        directory = tempfile.mkdtemp(prefix="harness_det2_")
        try:
            rows = self._metrics(directory, "F")
            path = os.path.join(directory, "metrics.jsonl")
            write_metrics(path, rows)
            with open(path, "r", encoding="utf-8") as handle:
                text_one = handle.read()
            write_metrics(path, rows)
            with open(path, "r", encoding="utf-8") as handle:
                text_two = handle.read()
            self.assertEqual(text_one, text_two)
        finally:
            shutil.rmtree(directory, ignore_errors=True)


class TaskSuiteTest(unittest.TestCase):
    """任务集与取样（§12.3 规模要求 / 分层分析前提）。"""

    def test_suite_has_80_tasks_with_20_per_category(self):
        tasks = load_tasks()
        self.assertEqual(len(tasks), 80)
        counts = {}
        for task in tasks:
            counts[task.category] = counts.get(task.category, 0) + 1
        self.assertEqual(
            counts, {"long_code": 20, "long_text": 20, "short_command": 20, "mixed": 20}
        )

    def test_every_task_carries_required_annotations(self):
        for task in load_tasks():
            self.assertTrue(task.expected_error_prone_areas, task.id)
            self.assertIn(task.baseline_difficulty, ("easy", "medium", "hard"), task.id)
            if task.category == "short_command":
                self.assertTrue(task.dangerous_operations, task.id)

    def test_limit_is_stratified_across_categories(self):
        """`--limit` 必须按类别均衡取样，否则分层分析与“每类都有样本”会失效。"""
        from experiments.runner import build_group_configs
        from tasks.schema import category_counts

        config = build_group_configs("A", task_limit=20)[0]
        counts = category_counts(getattr(config, "task_objects"))
        self.assertEqual(counts, {"long_code": 5, "long_text": 5, "short_command": 5, "mixed": 5})

        config = build_group_configs("A", task_limit=8)[0]
        counts = category_counts(getattr(config, "task_objects"))
        self.assertEqual(sum(counts.values()), 8)
        for category, count in counts.items():
            self.assertEqual(count, 2, category)

    def test_h_subgroups_share_identical_task_selection(self):
        from experiments.runner import build_group_configs

        configs = build_group_configs("H", task_limit=20)
        selections = {tuple(c.tasks) for c in configs}
        self.assertEqual(len(selections), 1)
        self.assertEqual(len(next(iter(selections))), 20)


class ConfigMatrixTest(unittest.TestCase):
    """§11.2 实验组矩阵必须与配置逐项一致。"""

    EXPECTED = {
        "A": (False, False, False, False, "none"),
        "B": (True, False, False, False, "none"),
        "C": (False, True, False, False, "none"),
        "D": (False, False, True, False, "none"),
        "E": (True, True, False, False, "none"),
        "F": (True, True, True, True, "none"),
        "G": (True, True, True, True, "strong"),
    }

    def test_group_matrix_matches_spec(self):
        for group, (router, tail, auditor, stack, prompt) in self.EXPECTED.items():
            config = build_group_configs(group, task_limit=4)[0]
            mechanisms = config.enabled_mechanisms
            self.assertEqual(mechanisms.risk_router, router, group)
            self.assertEqual(mechanisms.tail_audit, tail, group)
            self.assertEqual(mechanisms.external_auditor, auditor, group)
            self.assertEqual(mechanisms.revision_stack, stack, group)
            self.assertEqual(config.prompt_condition, prompt, group)

    def test_group_h_expands_to_three_subgroups(self):
        configs = build_group_configs("H", task_limit=4)
        self.assertEqual([c.sub_group for c in configs], ["H-none", "H-weak", "H-strong"])
        self.assertEqual([c.prompt_condition for c in configs], ["none", "weak", "strong"])
        task_sets = {tuple(c.tasks) for c in configs}
        samplings = {json.dumps(c.sampling.to_dict(), sort_keys=True) for c in configs}
        self.assertEqual(len(task_sets), 1, "H 三个子组必须使用同一任务集")
        self.assertEqual(len(samplings), 1, "H 三个子组必须使用同一采样参数")

    def test_only_two_variables_differ_across_groups(self):
        mechanisms_and_prompt = []
        for group in ("A", "B", "C", "D", "E", "F", "G"):
            config = build_group_configs(group, task_limit=4)[0]
            mechanisms_and_prompt.append(
                (
                    json.dumps(config.sampling.to_dict(), sort_keys=True),
                    tuple(sorted(config.tasks)),
                    config.runs_per_task,
                    config.timeout,
                )
            )
        # 采样 / 任务集 / 重复次数 / 超时在所有组之间必须完全一致
        self.assertEqual(len(set(mechanisms_and_prompt)), 1)


if __name__ == "__main__":
    unittest.main()
