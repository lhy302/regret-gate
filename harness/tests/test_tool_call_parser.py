"""模块级验收：`tool_call_parser`（构建规范 §14.1 / §3.3）。

必须覆盖：正确解析 OpenAI 与 Anthropic 两种格式；无效 JSON 返回 `malformed`。
"""

from __future__ import annotations

import json
import unittest

from core.stream_collector import StreamCollector
from core.tool_call_parser import ToolCallParser, default_registry, tools_payload
from core.types import StreamChunk


class RegistryTest(unittest.TestCase):
    def test_required_tools_registered(self):
        registry = default_registry()
        for name in ("read_file", "write_file", "execute_shell", "http_request", "draft.commit_revision"):
            self.assertIn(name, registry)

    def test_effect_types(self):
        registry = default_registry()
        self.assertEqual(registry["read_file"].effect_type, "read_only")
        self.assertEqual(registry["write_file"].effect_type, "bufferable")
        self.assertEqual(registry["execute_shell"].effect_type, "irreversible")
        self.assertEqual(registry["http_request"].effect_type, "externalized")
        self.assertEqual(registry["draft.commit_revision"].effect_type, "read_only")

    def test_draft_commit_revision_schema(self):
        schema = default_registry()["draft.commit_revision"].parameters
        self.assertEqual(set(schema["required"]), {"target_text", "op"})
        self.assertEqual(set(schema["properties"]["op"]["enum"]), {"insert", "erase", "replace"})
        self.assertEqual(schema["properties"]["reason"]["maxLength"], 200)

    def test_tools_payload_shape(self):
        payload = tools_payload()
        self.assertEqual(len(payload), 5)
        self.assertEqual(payload[0]["type"], "function")
        self.assertIn("name", payload[0]["function"])


class ProvidersNormalizationTest(unittest.TestCase):
    """OpenAI 与 Anthropic 两种流式形态必须归一化成同一结果。"""

    def test_openai_style_stream(self):
        chunks = [
            StreamChunk(kind="text", text="Let me write..."),
            StreamChunk(kind="tool_call_start", tool_call_id="call_1", tool_name="execute_shell"),
            StreamChunk(kind="tool_call_delta", tool_call_id="call_1", tool_args_delta='{"comm'),
            StreamChunk(kind="tool_call_delta", tool_call_id="call_1", tool_args_delta='and": "ls"}'),
            StreamChunk(kind="tool_call_end", tool_call_id="call_1", tool_name="execute_shell"),
            StreamChunk(kind="done"),
        ]
        collector = StreamCollector()
        for chunk in chunks:
            collector.feed(chunk)
        self.assertEqual(collector.text, "Let me write...")
        self.assertEqual(len(collector.tool_calls), 1)
        parsed = ToolCallParser().parse(collector.tool_calls[0])
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.call.args, {"command": "ls"})

    def test_anthropic_style_stream_with_structured_args(self):
        chunks = [
            StreamChunk(kind="text", text="checking"),
            StreamChunk(kind="tool_call_start", tool_call_id="toolu_1", tool_name="read_file"),
            StreamChunk(
                kind="tool_call_end",
                tool_call_id="toolu_1",
                tool_name="read_file",
                tool_args={"path": "a.txt"},
            ),
            StreamChunk(kind="done"),
        ]
        collector = StreamCollector()
        for chunk in chunks:
            collector.feed(chunk)
        parsed = ToolCallParser().parse(collector.tool_calls[0])
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.call.args, {"path": "a.txt"})
        self.assertEqual(parsed.call.id, "toolu_1")

    def test_both_providers_produce_equivalent_parse_result(self):
        openai = ToolCallParser().parse(
            StreamCollector()._end(
                StreamChunk(kind="tool_call_end", tool_call_id="c", tool_name="write_file",
                            tool_args={"path": "p", "content": "c"})
            )
        )
        anthropic = ToolCallParser().parse(
            StreamCollector()._end(
                StreamChunk(kind="tool_call_end", tool_call_id="c", tool_name="write_file",
                            tool_args=json.dumps({"path": "p", "content": "c"}))
            )
        )
        self.assertEqual(openai.call.args, anthropic.call.args)


class MalformedTest(unittest.TestCase):
    def test_invalid_json_marked_malformed(self):
        call = StreamCollector()._end(
            StreamChunk(kind="tool_call_end", tool_call_id="c1", tool_name="execute_shell",
                        tool_args='{"command": "ls"')
        )
        self.assertTrue(call.malformed)
        result = ToolCallParser().parse(call)
        self.assertFalse(result.ok)
        self.assertIn("invalid json", result.reason)
        self.assertEqual(result.raw_args, '{"command": "ls"')

    def test_empty_arguments_marked_malformed(self):
        call = StreamCollector()._end(
            StreamChunk(kind="tool_call_end", tool_call_id="c1", tool_name="execute_shell", tool_args=None)
        )
        self.assertTrue(call.malformed)
        self.assertFalse(ToolCallParser().parse(call).ok)

    def test_non_object_arguments_marked_malformed(self):
        call = StreamCollector()._end(
            StreamChunk(kind="tool_call_end", tool_call_id="c1", tool_name="execute_shell", tool_args="[1,2]")
        )
        self.assertTrue(call.malformed)

    def test_malformed_does_not_block_stream(self):
        collector = StreamCollector()
        collector.feed(StreamChunk(kind="tool_call_start", tool_call_id="c1", tool_name="execute_shell"))
        collector.feed(StreamChunk(kind="tool_call_delta", tool_call_id="c1", tool_args_delta="{broken"))
        collector.feed(StreamChunk(kind="tool_call_end", tool_call_id="c1", tool_name="execute_shell"))
        collector.feed(StreamChunk(kind="text", text="continued"))
        collector.feed(StreamChunk(kind="done"))
        self.assertEqual(len(collector.malformed), 1)
        self.assertEqual(collector.text, "continued")


class SchemaValidationTest(unittest.TestCase):
    def setUp(self):
        self.parser = ToolCallParser()

    def _call(self, name, args):
        return StreamCollector()._end(
            StreamChunk(kind="tool_call_end", tool_call_id="c", tool_name=name, tool_args=args)
        )

    def test_unknown_tool_rejected(self):
        raw = StreamCollector()._end(
            StreamChunk(kind="tool_call_end", tool_call_id="c", tool_name="nope", tool_args={})
        )
        result = self.parser.parse(raw)
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_field, "tool_name")

    def test_missing_required_field(self):
        result = self.parser.parse(self._call("read_file", {}))
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_field, "path")
        self.assertIn("required", result.reason)

    def test_unexpected_field_rejected(self):
        result = self.parser.parse(self._call("read_file", {"path": "a", "bogus": 1}))
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_field, "bogus")

    def test_wrong_type(self):
        result = self.parser.parse(self._call("read_file", {"path": 123}))
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_field, "path")

    def test_enum_violation(self):
        result = self.parser.parse(
            self._call("draft.commit_revision", {"target_text": "a", "op": "patch", "payload": "b"})
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_field, "op")
        self.assertIn("enum", result.reason)

    def test_max_length_violation(self):
        result = self.parser.parse(
            self._call(
                "draft.commit_revision",
                {"target_text": "a", "op": "replace", "payload": "b", "reason": "x" * 201},
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_field, "reason")

    def test_valid_draft_call_ok(self):
        result = self.parser.parse(
            self._call(
                "draft.commit_revision",
                {"target_text": "a", "op": "insert", "payload": "b", "reason": "why"},
            )
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.call.args["op"], "insert")

    def test_parse_never_raises(self):
        for args in (None, "string", 5, [], {"path": object()}):
            try:
                self.parser.parse(self._call("read_file", args))
            except Exception as exc:  # noqa: BLE001
                self.fail(f"parse must not raise, got {exc!r}")


class StreamEdgeCaseTest(unittest.TestCase):
    def test_duplicate_tool_call_id_deduplicated(self):
        collector = StreamCollector()
        for _ in range(2):
            collector.feed(StreamChunk(kind="tool_call_start", tool_call_id="dup", tool_name="read_file"))
            collector.feed(StreamChunk(kind="tool_call_end", tool_call_id="dup", tool_name="read_file",
                                       tool_args={"path": "a"}))
        self.assertEqual(len(collector.tool_calls), 1)

    def test_interleaved_text_and_tool_calls_keep_order(self):
        collector = StreamCollector()
        collector.feed(StreamChunk(kind="text", text="first"))
        collector.feed(StreamChunk(kind="tool_call_start", tool_call_id="c1", tool_name="read_file"))
        collector.feed(StreamChunk(kind="tool_call_end", tool_call_id="c1", tool_name="read_file",
                                   tool_args={"path": "a"}))
        collector.feed(StreamChunk(kind="text", text="second"))
        collector.feed(StreamChunk(kind="done"))
        kinds = [kind for kind, _ in collector.ordered_events]
        self.assertEqual(kinds, ["text", "tool_call", "text"])

    def test_stream_error_marks_incomplete(self):
        collector = StreamCollector()
        collector.feed(StreamChunk(kind="text", text="partial"))
        collector.feed(StreamChunk(kind="error", error="connection reset"))
        self.assertTrue(collector.incomplete)
        self.assertEqual(collector.errors, ["connection reset"])
        self.assertEqual(collector.text, "partial")

    def test_unclosed_tool_call_recovered_at_stream_end(self):
        collector = StreamCollector()
        collector.feed(StreamChunk(kind="tool_call_start", tool_call_id="c1", tool_name="read_file"))
        collector.feed(StreamChunk(kind="tool_call_delta", tool_call_id="c1", tool_args_delta='{"path": "a"}'))
        collector.feed(StreamChunk(kind="done"))
        self.assertEqual(len(collector.tool_calls), 1)
        self.assertEqual(collector.tool_calls[0].args, {"path": "a"})


if __name__ == "__main__":
    unittest.main()
