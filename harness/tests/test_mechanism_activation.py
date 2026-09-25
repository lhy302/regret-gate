"""机制级验收：D 组必须真的调用独立审核 Agent（§11.2 / §8.1）。

`external_auditor` 是机制 3 的唯一开关。如果 D 组因为“没有 RiskRouter ⇒ 没有
intercept ⇒ 没有待审操作”而一次审核都没发起，那么 D 组测的就不是“独立审核的效果”，
而是“什么都没发生”——这类静默空转必须在测试里锁死。
"""

from __future__ import annotations

import unittest

from core.audit_logger import NullAuditLogger
from core.types import EnabledMechanisms, ExperimentConfig, SamplingConfig
from experiments.harness import MechanismHarness
from llm.fake_client import FakeClient
from tasks.loader import load_tasks


def config(**mechanisms) -> ExperimentConfig:
    return ExperimentConfig(
        group="X",
        prompt_condition="none",
        enabled_mechanisms=EnabledMechanisms(**mechanisms),
        sampling=SamplingConfig(model="fake-model-v1", temperature=0.0, top_p=1.0, seed=0),
        timeout=300.0,
    )


def task(task_id: str):
    return {t.id: t for t in load_tasks()}[task_id]


class AuditorActivationTest(unittest.TestCase):
    def _run(self, cfg, task_id="sc_001", verdict="approve"):
        target = task(task_id)
        main = FakeClient(tail_audit_verdict=verdict)
        main.set_task(target)
        auditor = FakeClient(tail_audit_verdict=verdict)
        auditor.set_task(target)
        logger = NullAuditLogger(run_id="x", group=cfg.group, task_id=target.id)
        harness = MechanismHarness(
            config=cfg, task=target, client=main, auditor_client=auditor, audit_logger=logger
        )
        return harness.run(), logger, auditor

    def test_d_group_actually_calls_the_auditor(self):
        """D 组（只有 external_auditor）必须发起审核请求，否则机制空转。"""
        result, logger, auditor = self._run(config(external_auditor=True))
        self.assertTrue(logger.events("risk_decision"), "D 组也要对待决操作做风险判定")
        self.assertTrue(logger.events("external_audit_request"), "D 组必须真正调用独立审核")
        self.assertGreaterEqual(result.external_audit_requests, 1)
        self.assertGreaterEqual(auditor.call_count, 1)
        self.assertEqual(auditor.calls[0]["role"], "auditor")

    def test_group_a_and_c_never_call_the_auditor(self):
        for mechanisms in (dict(), dict(tail_audit=True)):
            result, logger, auditor = self._run(config(**mechanisms))
            self.assertFalse(logger.events("external_audit_request"))
            self.assertEqual(result.external_audit_requests, 0)
            self.assertEqual(auditor.call_count, 0)

    def test_reject_verdict_blocks_execution(self):
        result, logger, _ = self._run(config(external_auditor=True), verdict="reject")
        self.assertEqual(result.executed_actions, 0)
        self.assertGreaterEqual(result.external_audit_rejected, 1)
        self.assertFalse(result.security_incident)
        resolutions = [e for e in logger.events("pending_resolved")
                       if e["data"].get("resolution") == "rejected"]
        self.assertTrue(resolutions)

    def test_reject_verdict_marks_audit_effective_when_true_positive(self):
        """拦截的是任务声明的高危操作 → 独立审核记为“有效拦截”。"""
        result, _, _ = self._run(config(external_auditor=True), verdict="reject")
        self.assertTrue(result.external_audit_effective)

    def test_unparseable_audit_response_degrades_to_needs_revision(self):
        target = task("sc_001")
        main = FakeClient()
        main.set_task(target)
        auditor = FakeClient(audit_raw_override="我不能给出 JSON")
        auditor.set_task(target)
        harness = MechanismHarness(config=config(external_auditor=True), task=target, client=main,
                                   auditor_client=auditor)
        result = harness.run()
        # needs_revision 不是 approve → 不执行
        self.assertEqual(result.executed_actions, 0)
        self.assertEqual(result.error, None)

    def test_audit_request_carries_no_conversation_history(self):
        result, logger, _ = self._run(config(external_auditor=True))
        for event in logger.events("external_audit_request"):
            data = event["data"]
            self.assertFalse(data.get("conversation_history_sent"))
            self.assertEqual(set(data["action"].keys()), {"tool_name", "args"})
            self.assertTrue(data["checklist"])


if __name__ == "__main__":
    unittest.main()
