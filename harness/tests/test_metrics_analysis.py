"""模块级验收：`metrics` / `stats` / `analysis` / `report`（构建规范 §13）。

这里专门锁住一类**静默错误**：布尔指标被统一转成 1，导致汇总表看起来“全组都完美”。
"""

from __future__ import annotations

import unittest

from experiments.analysis import (
    build_analysis,
    evaluate_hypotheses,
    false_intercept_rate,
    group_summaries,
    token_cost_delta,
)
from experiments.metrics import aggregate, run_metrics_from_result
from experiments.report import build_report
from experiments.stats import (
    betainc,
    describe,
    metric_values,
    paired_compare,
    paired_test,
    stratified_by_category,
    student_t_cdf,
    task_level_means,
)


def make_rows() -> list:
    """构造一组已知答案的假指标：A 组差、F 组好，B 组有误拦截。"""
    rows = []
    for index in range(10):
        task_id = f"lc_{index:03d}"
        rows.append(
            {
                "run_id": f"A-{task_id}-r0",
                "group": "A",
                "task_id": task_id,
                "category": "long_code",
                "syntax_error": index < 8,          # 8/10
                "logic_error": index < 6,           # 6/10
                "task_success": index >= 8,         # 2/10
                "security_incident": index < 3,     # 3/10
                "false_intercept": False,
                "input_tokens": 100,
                "output_tokens": 100,
                "latency_ms": 10,
                "tail_audit_modified": False,
                "external_audit_effective": False,
                "revision_hit_rate": 0.0,
                "cache_break_ratio": 0.2,
                "intercepts": 0,
                "intercept_rules": [],
                "llm_calls": 2,
                "audit_log_bytes": 1000,
                "error": None,
            }
        )
        rows.append(
            {
                "run_id": f"F-{task_id}-r0",
                "group": "F",
                "task_id": task_id,
                "category": "long_code",
                "syntax_error": index < 2,          # 2/10
                "logic_error": index < 2,           # 2/10
                "task_success": index >= 2,         # 8/10
                "security_incident": False,
                "false_intercept": False,
                "input_tokens": 150,
                "output_tokens": 150,
                "latency_ms": 20,
                "tail_audit_modified": index < 7,   # 7/10
                "external_audit_effective": True,
                "revision_hit_rate": 1.0,
                "cache_break_ratio": 0.25,
                "intercepts": 1,
                "intercept_rules": ["shell_destructive_intercept"],
                "llm_calls": 6,
                "audit_log_bytes": 5000,
                "error": None,
            }
        )
    return rows


class MetricValuesTest(unittest.TestCase):
    def test_false_becomes_zero_not_one(self):
        rows = [{"flag": False}, {"flag": True}, {"flag": False}]
        self.assertEqual(metric_values(rows, "flag"), [0.0, 1.0, 0.0])

    def test_none_skipped(self):
        rows = [{"flag": None}, {"flag": True}]
        self.assertEqual(metric_values(rows, "flag"), [1.0])

    def test_numeric_passthrough(self):
        rows = [{"n": 3}, {"n": 7}]
        self.assertEqual(metric_values(rows, "n"), [3.0, 7.0])

    def test_aggregate_handles_booleans(self):
        rows = [{"flag": False}, {"flag": True}]
        self.assertAlmostEqual(aggregate(rows, "flag")["mean"], 0.5)


class DescribeTest(unittest.TestCase):
    def test_mean_median_stdev(self):
        result = describe([1.0, 2.0, 3.0, 4.0])
        self.assertAlmostEqual(result["mean"], 2.5)
        self.assertAlmostEqual(result["median"], 2.5)
        self.assertGreater(result["stdev"], 0)
        self.assertEqual(result["min"], 1.0)
        self.assertEqual(result["max"], 4.0)

    def test_ci_brackets_mean(self):
        result = describe([1.0, 2.0, 3.0, 4.0, 5.0])
        low, high = result["ci95"]
        self.assertLess(low, result["mean"])
        self.assertGreater(high, result["mean"])

    def test_empty_input(self):
        result = describe([])
        self.assertEqual(result["n"], 0)
        self.assertIsNone(result["mean"])


class StudentTTest(unittest.TestCase):
    def test_cdf_symmetry(self):
        self.assertAlmostEqual(student_t_cdf(0.0, 10), 0.5, places=6)
        self.assertAlmostEqual(student_t_cdf(2.0, 10) + student_t_cdf(-2.0, 10), 1.0, places=6)

    def test_known_value(self):
        # t=2.228, df=10 → 双侧 95% 临界值，CDF ≈ 0.975
        self.assertAlmostEqual(student_t_cdf(2.228, 10), 0.975, places=3)

    def test_betainc_bounds(self):
        self.assertEqual(betainc(1.0, 1.0, 0.0), 0.0)
        self.assertEqual(betainc(1.0, 1.0, 1.0), 1.0)
        self.assertAlmostEqual(betainc(1.0, 1.0, 0.5), 0.5, places=6)


class PairedTestTest(unittest.TestCase):
    def test_shift_detected(self):
        a = [1.0] * 10
        b = [0.0] * 10
        result = paired_test(a, b, "A", "B")
        self.assertEqual(result["n"], 10)
        self.assertAlmostEqual(result["mean_diff"], -1.0)
        self.assertLess(result["p"], 0.05)
        self.assertEqual(result["cohen_dz"], float("inf"))  # sd_diff == 0 且均值差不为 0

    def test_no_difference(self):
        result = paired_test([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
        self.assertAlmostEqual(result["mean_diff"], 0.0)
        self.assertEqual(result["p"], 1.0)

    def test_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            paired_test([1.0], [1.0, 2.0])

    def test_task_level_means_aggregates_runs(self):
        rows = [
            {"task_id": "t1", "flag": True},
            {"task_id": "t1", "flag": False},
            {"task_id": "t2", "flag": True},
        ]
        means = task_level_means(rows, "flag")
        self.assertAlmostEqual(means["t1"], 0.5)
        self.assertAlmostEqual(means["t2"], 1.0)

    def test_paired_compare_uses_shared_tasks_only(self):
        rows = [
            {"group": "A", "task_id": "t1", "flag": True},
            {"group": "A", "task_id": "t2", "flag": True},
            {"group": "F", "task_id": "t1", "flag": False},
        ]
        result = paired_compare(rows, "A", "F", "flag")
        self.assertEqual(result["n"], 1)
        self.assertEqual(result["shared_tasks"], ["t1"])


class GroupSummariesTest(unittest.TestCase):
    def test_means_match_raw_counts(self):
        summaries = group_summaries(make_rows())
        self.assertAlmostEqual(summaries["A"]["metrics"]["syntax_error"]["mean"], 0.8)
        self.assertAlmostEqual(summaries["F"]["metrics"]["syntax_error"]["mean"], 0.2)
        self.assertAlmostEqual(summaries["A"]["metrics"]["task_success"]["mean"], 0.2)
        self.assertAlmostEqual(summaries["F"]["metrics"]["task_success"]["mean"], 0.8)
        self.assertAlmostEqual(summaries["A"]["metrics"]["security_incident"]["mean"], 0.3)

    def test_summary_reports_distribution_not_only_mean(self):
        entry = group_summaries(make_rows())["A"]["metrics"]["syntax_error"]
        for key in ("mean", "median", "stdev", "min", "max", "ci95", "n"):
            self.assertIn(key, entry)

    def test_rule_stats_flag_high_false_intercept(self):
        rows = [
            {"group": "B", "intercepts": 1, "intercept_rules": ["r"], "false_intercept": True,
             "false_intercept_rule": "r"},
            {"group": "B", "intercepts": 1, "intercept_rules": ["r"], "false_intercept": False,
             "false_intercept_rule": None},
        ]
        stats = group_summaries(rows)["B"]["rules"]["r"]
        self.assertEqual(stats["intercepts"], 2)
        self.assertEqual(stats["false"], 1)
        self.assertAlmostEqual(stats["false_intercept_rate"], 0.5)
        self.assertTrue(stats["needs_rework"])


class FalseInterceptRateTest(unittest.TestCase):
    def test_none_when_no_intercepts(self):
        self.assertIsNone(false_intercept_rate([{"intercepts": 0, "false_intercept": False}])["rate"])

    def test_ratio(self):
        rows = [
            {"intercepts": 2, "false_intercept": True},
            {"intercepts": 2, "false_intercept": False},
        ]
        result = false_intercept_rate(rows)
        self.assertEqual(result["total_intercepts"], 4)
        self.assertEqual(result["false_intercepts"], 2)
        self.assertAlmostEqual(result["rate"], 0.5)


class TokenAndLatencyDeltaTest(unittest.TestCase):
    def test_delta_relative_to_baseline(self):
        rows = [
            {"group": "A", "input_tokens": 100, "output_tokens": 100, "latency_ms": 10},
            {"group": "F", "input_tokens": 200, "output_tokens": 200, "latency_ms": 30},
        ]
        result = token_cost_delta(rows, "A")
        self.assertAlmostEqual(result["baseline_mean_tokens"], 200.0)
        self.assertAlmostEqual(result["by_group"]["F"]["delta_tokens"], 200.0)
        self.assertAlmostEqual(result["by_group"]["F"]["delta_ratio"], 2.0)

    def test_no_baseline_is_safe(self):
        rows = [{"group": "F", "input_tokens": 10, "output_tokens": 10, "latency_ms": 1}]
        result = token_cost_delta(rows, "A")
        self.assertEqual(result["baseline_mean_tokens"], 0.0)


class AnalysisAndReportTest(unittest.TestCase):
    def test_hypotheses_cover_h1_to_h6(self):
        hypotheses = evaluate_hypotheses(make_rows())
        self.assertEqual([h["id"] for h in hypotheses], ["H1", "H2", "H3", "H4", "H5", "H6"])
        for item in hypotheses:
            self.assertIn("statement", item)
            self.assertIn("falsified_if", item)

    def test_h1_supported_by_constructed_data(self):
        hypotheses = {h["id"]: h for h in evaluate_hypotheses(make_rows())}
        self.assertTrue(hypotheses["H1"]["supported"])
        self.assertLess(hypotheses["H1"]["test"]["mean_diff"], 0)
        self.assertLess(hypotheses["H1"]["test"]["p"], 0.05)

    def test_stratified_by_category(self):
        result = stratified_by_category(make_rows(), "syntax_error")
        self.assertIn("long_code", result)
        self.assertAlmostEqual(result["long_code"]["A"]["mean"], 0.8)

    def test_report_contains_all_required_sections(self):
        report = build_report(make_rows(), meta={"provider": "fake"})
        for section in ("## 1. 每组指标汇总", "## 2. 指标分布", "## 3. 关键对比的配对检验",
                        "## 4. 假设 H1–H6 判定", "## 5. 误拦截率与规则级统计",
                        "## 6. 成本与延迟", "## 7. 缓存破坏点分布", "## 8. 分层分析",
                        "## 9. 失败与未成功案例分析", "## 10. 性能与预算验收",
                        "## 11. 结论与下一步建议"):
            self.assertIn(section, report)

    def test_report_marks_offline_data_source(self):
        report = build_report(make_rows())
        self.assertIn("FakeClient", report)
        self.assertIn("不构成对模型能力", report)

    def test_report_flags_false_intercept_rule(self):
        rows = make_rows()
        rows[0]["false_intercept"] = True
        rows[0]["false_intercept_rule"] = "shell_destructive_intercept"
        report = build_report(rows)
        self.assertIn("shell_destructive_intercept", report)

    def test_analysis_json_serializable(self):
        import json

        analysis = build_analysis(make_rows())
        json.dumps(analysis, ensure_ascii=False, default=str)


class MetricsFromResultTest(unittest.TestCase):
    class FakeResult:
        run_id = "F-lc_001-r0"
        group = "F"
        task_id = "lc_001"
        category = "long_code"
        prompt_condition = "none"
        syntax_error = False
        logic_error = False
        security_incident = False
        false_intercept = False
        token_usage = {"input_tokens": 10, "output_tokens": 20}
        latency_ms = 5
        tail_audit_modified = True
        external_audit_effective = False
        revision_hit_rate = 1.0
        task_success = True
        cache_break_ratio = 0.1
        error = None
        intercepts = 1
        pending_actions = [
            {"risk": "intercept", "matched_rule": "shell_destructive_intercept", "status": "audited",
             "result": None},
            {"risk": "buffer", "matched_rule": "write_file_buffer", "status": "audited", "result": None},
            {"risk": "allow", "matched_rule": None, "status": "audited", "result": None},
        ]
        revisions = [
            {"id": "r1", "status": "applied"},
            {"id": "r2", "status": "rejected"},
        ]
        false_intercept_rule = None
        llm_calls = 6
        tail_audit_injections = 2
        tail_audit_timed_out = False
        tail_audit_infrastructure_error = False
        external_audit_requests = 1
        external_audit_rejected = 0
        executed_actions = 2
        cache_break_point = 100
        finalized_text = "x" * 50
        validation = {"passed": True}

    def test_field_mapping(self):
        metrics = run_metrics_from_result(self.FakeResult())
        self.assertEqual(metrics["intercepts"], 1)
        self.assertEqual(metrics["buffers"], 1)
        self.assertEqual(metrics["revisions_applied"], 1)
        self.assertEqual(metrics["revisions_rejected"], 1)
        self.assertEqual(metrics["intercept_rules"], ["shell_destructive_intercept"])
        self.assertEqual(metrics["finalized_text_len"], 50)
        self.assertTrue(metrics["validation_passed"])
        self.assertEqual(metrics["input_tokens"], 10)
        self.assertEqual(metrics["output_tokens"], 20)


if __name__ == "__main__":
    unittest.main()
