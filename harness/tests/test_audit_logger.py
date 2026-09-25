"""模块级验收：`audit_logger` 与 `pending_actions`（构建规范 §14.1 / §5 / §10）。"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest

from core.audit_logger import EVENT_TYPES, AuditLogger, NullAuditLogger
from core.pending_actions import PendingActions
from core.types import (
    PENDING_APPROVED,
    PENDING_AUDITED,
    PENDING_EXECUTED,
    PENDING_PENDING,
    PENDING_REJECTED,
    PENDING_REVISED,
    RISK_BUFFER,
    RISK_INTERCEPT,
    RiskDecision,
    ToolCall,
    ToolResult,
)


def call(name="execute_shell", **args):
    return ToolCall(id="c1", name=name, args=args, raw_args="")


def decision(level=RISK_INTERCEPT, reason="destructive"):
    return RiskDecision(level=level, reason=reason, matched_rule="rule:x", rule_id="rule:x")


class AuditLoggerFormatTest(unittest.TestCase):
    def test_jsonl_line_format(self):
        buffer = io.StringIO()
        logger = AuditLogger(stream=buffer, run_id="r1", group="F", task_id="sc_001", clock=lambda: 123.5)
        logger.log("turn_start", turn=0, data={"k": "v"})
        line = buffer.getvalue().strip()
        record = json.loads(line)
        self.assertEqual(set(record.keys()), {"ts", "turn", "event", "run_id", "group", "task_id", "data"})
        self.assertEqual(record["ts"], 123.5)
        self.assertEqual(record["turn"], 0)
        self.assertEqual(record["event"], "turn_start")
        self.assertEqual(record["run_id"], "r1")
        self.assertEqual(record["group"], "F")
        self.assertEqual(record["task_id"], "sc_001")
        self.assertEqual(record["data"], {"k": "v"})

    def test_every_spec_event_type_accepted(self):
        logger = NullAuditLogger(run_id="r", group="A", task_id="t")
        for event in EVENT_TYPES:
            logger.log(event, turn=0)
        self.assertEqual(len(logger.records), len(EVENT_TYPES))

    def test_unknown_event_rejected(self):
        logger = NullAuditLogger(run_id="r", group="A", task_id="t")
        with self.assertRaises(ValueError):
            logger.log("not_an_event")

    def test_dataclass_data_is_serialized(self):
        buffer = io.StringIO()
        logger = AuditLogger(stream=buffer, run_id="r", group="A", task_id="t")
        logger.log("risk_decision", turn=0, data={"decision": decision()})
        record = json.loads(buffer.getvalue().strip())
        self.assertEqual(record["data"]["decision"]["level"], RISK_INTERCEPT)


class AuditLoggerImmutabilityTest(unittest.TestCase):
    def test_appends_never_truncates_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "audit.jsonl")
            first = AuditLogger(path=path, run_id="r1", group="A", task_id="t")
            first.log("turn_start", turn=0, data={"n": 1})
            first.close()
            second = AuditLogger(path=path, run_id="r2", group="A", task_id="t")
            second.log("turn_start", turn=0, data={"n": 2})
            second.close()
            with open(path, "r", encoding="utf-8") as handle:
                lines = [json.loads(line) for line in handle if line.strip()]
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0]["run_id"], "r1")
            self.assertEqual(lines[1]["run_id"], "r2")

    def test_writes_are_line_delimited_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "a.jsonl")
            logger = AuditLogger(path=path, run_id="r", group="A", task_id="t")
            logger.log("turn_start", turn=0)
            logger.log("turn_end", turn=1)
            logger.close()
            with open(path, "r", encoding="utf-8") as handle:
                content = handle.read()
            self.assertEqual(len(content.strip().split("\n")), 2)

    def test_closed_logger_refuses_further_writes(self):
        logger = NullAuditLogger(run_id="r", group="A", task_id="t")
        logger.close()
        with self.assertRaises(RuntimeError):
            logger.log("turn_start")

    def test_timeline_reconstructs_order(self):
        logger = NullAuditLogger(run_id="r", group="F", task_id="sc_001")
        for event in ("turn_start", "risk_decision", "pending_added", "tail_audit_injected",
                      "tail_audit_response", "finalize_done", "external_audit_request",
                      "external_audit_response", "pending_resolved", "tool_executed", "turn_end"):
            logger.log(event, turn=0)
        timeline = logger.timeline()
        self.assertEqual([t["seq"] for t in timeline], list(range(11)))
        self.assertEqual(timeline[0]["event"], "turn_start")
        self.assertEqual(timeline[-1]["event"], "turn_end")


class PendingActionsStateMachineTest(unittest.TestCase):
    def setUp(self):
        self.pending = PendingActions(turn=0)
        self.pending_id = self.pending.add(call(), decision(), step=1)

    def test_add_returns_id_and_starts_pending(self):
        self.assertTrue(self.pending_id)
        self.assertEqual(self.pending.get(self.pending_id).status, PENDING_PENDING)
        self.assertEqual(len(self.pending.unresolved()), 1)

    def test_approve_execute_audit_flow(self):
        self.assertIsNotNone(self.pending.resolve(self.pending_id, PENDING_APPROVED, step=2))
        result = ToolResult(tool_call_id="c1", tool_name="execute_shell", ok=True, executed=False, dry_run=True)
        self.pending.attach_result(self.pending_id, result, step=3)
        item = self.pending.get(self.pending_id)
        self.assertEqual(item.status, PENDING_AUDITED)
        self.assertIsNotNone(item.result)

    def test_reject_then_audit(self):
        self.assertIsNotNone(self.pending.resolve(self.pending_id, PENDING_REJECTED, step=2))
        self.pending.resolve(self.pending_id, PENDING_AUDITED, step=3)
        self.assertEqual(self.pending.get(self.pending_id).status, PENDING_AUDITED)

    def test_revised_returns_to_pending_then_reviewable(self):
        self.assertIsNotNone(self.pending.resolve(self.pending_id, PENDING_REVISED, step=2))
        self.assertEqual(self.pending.get(self.pending_id).status, PENDING_REVISED)
        self.assertIsNotNone(self.pending.resolve(self.pending_id, PENDING_APPROVED, step=3))

    def test_illegal_transition_returns_none_without_raising(self):
        self.pending.resolve(self.pending_id, PENDING_REJECTED, step=2)
        self.assertIsNone(self.pending.resolve(self.pending_id, PENDING_EXECUTED, step=3))
        self.assertEqual(self.pending.get(self.pending_id).status, PENDING_REJECTED)

    def test_unknown_id_returns_none(self):
        self.assertIsNone(self.pending.resolve("nope", PENDING_APPROVED))

    def test_by_level_and_intercepts(self):
        self.pending.add(call(command="echo hi"), decision(level=RISK_BUFFER, reason="bufferable"), step=4)
        self.assertEqual(len(self.pending.intercepts()), 1)
        self.assertEqual(len(self.pending.by_level(RISK_BUFFER)), 1)

    def test_snapshot_keeps_resolution_history(self):
        self.pending.resolve(self.pending_id, PENDING_REJECTED, step=2, audit_verdict="reject")
        snapshot = self.pending.snapshot()
        item = snapshot["items"][0]
        self.assertEqual(item["status"], PENDING_REJECTED)
        self.assertEqual(item["audit_verdict"], "reject")
        self.assertEqual(len(item["resolution_history"]), 2)

    def test_resolution_history_records_audit_issues(self):
        self.pending.resolve(
            self.pending_id, PENDING_REJECTED, step=2, audit_verdict="reject",
            audit_issues=[{"severity": "high", "description": "d", "suggestion": "s"}],
        )
        item = self.pending.get(self.pending_id)
        self.assertEqual(item.audit_issues[0]["severity"], "high")


if __name__ == "__main__":
    unittest.main()
