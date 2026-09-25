"""流式收集器（构建规范 §2.3）。

必须处理的边界情况：

1. **工具调用参数分块到达**：绝不逐 delta 解析 JSON，只在 `tool_call_end` 时解析一次；
2. **部分 JSON 无效**：记录原始字符串，标记 `malformed`，不阻塞流；
3. **流中断**：记录已接收部分，标记 `incomplete`，由上层决定是否重试；
4. **重复工具调用 ID**：按 ID 去重（provider 偶尔重发）；
5. **文本与工具调用交错**：按到达顺序处理。
"""

from __future__ import annotations

import time
from typing import Iterator, Optional

from core.tool_call_parser import ToolCallParser, normalize_tool_call
from core.types import ToolCall, TokenUsage


class StreamCollector:
    """消费 `LLMClient.stream_chat` 的 chunk 流，产出文本与工具调用。"""

    def __init__(self, parser: Optional[ToolCallParser] = None):
        self.parser = parser if parser is not None else ToolCallParser()
        self.reset()

    # -- 生命周期 ---------------------------------------------------------

    def reset(self) -> None:
        self.text_parts: list = []
        self.tool_calls: list = []
        self.ordered_events: list = []
        self.malformed: list = []
        self.errors: list = []
        self.done = False
        self.incomplete = False
        self.usage = TokenUsage()
        self.saw_done = False
        self._pending: dict = {}
        self._seen_ids: set = set()
        self._seq = 0
        self._usage_seen = False

    # -- 消费 -------------------------------------------------------------

    def feed(self, chunk) -> list:
        """消费一个 chunk，返回本次新产生的 `ToolCall` 列表。"""
        kind = getattr(chunk, "kind", None)
        if kind == "text":
            text = chunk.text or ""
            if text:
                self.text_parts.append(text)
                self.ordered_events.append(("text", text))
            return []
        if kind == "tool_call_start":
            self._start(chunk)
            return []
        if kind == "tool_call_delta":
            self._delta(chunk)
            return []
        if kind == "tool_call_end":
            call = self._end(chunk)
            return [call] if call is not None else []
        if kind == "done":
            self.done = True
            self.saw_done = True
            if getattr(chunk, "usage", None) is not None:
                self._absorb_usage(chunk.usage)
            self._finalize_pending()
            return []
        if kind == "error":
            self.errors.append(getattr(chunk, "error", None) or "unknown stream error")
            self.incomplete = True
            return []
        self.errors.append(f"unknown chunk kind: {kind!r}")
        return []

    def consume(self, stream: Iterator) -> Iterator[ToolCall]:
        """迭代 chunk 流，逐个 yield 解析成功的工具调用。"""
        for chunk in stream:
            for call in self.feed(chunk):
                yield call

    def _absorb_usage(self, usage: TokenUsage) -> None:
        if self._usage_seen:
            # provider 可能只在最后一个 chunk 报总量；取最大值，不累加重复上报。
            self.usage.input_tokens = max(self.usage.input_tokens, usage.input_tokens)
            self.usage.output_tokens = max(self.usage.output_tokens, usage.output_tokens)
        else:
            self.usage = TokenUsage(usage.input_tokens, usage.output_tokens)
            self._usage_seen = True

    # -- 内部：工具调用累积 -----------------------------------------------

    def _start(self, chunk) -> None:
        call_id = chunk.tool_call_id or f"call_{self._seq}"
        entry = self._pending.setdefault(
            call_id, {"name": None, "args_str": [], "explicit_args": None, "order": None}
        )
        if entry["order"] is None:
            entry["order"] = self._seq
            self._seq += 1
        if chunk.tool_name:
            entry["name"] = chunk.tool_name

    def _delta(self, chunk) -> None:
        call_id = chunk.tool_call_id or f"call_{self._seq}"
        entry = self._pending.setdefault(
            call_id, {"name": None, "args_str": [], "explicit_args": None, "order": None}
        )
        if entry["order"] is None:
            entry["order"] = self._seq
            self._seq += 1
        if chunk.tool_name and not entry["name"]:
            entry["name"] = chunk.tool_name
        if chunk.tool_args_delta:
            entry["args_str"].append(chunk.tool_args_delta)
        if chunk.tool_args is not None and not isinstance(chunk.tool_args, str):
            # provider 已经给出结构化参数（非流式适配器）
            entry["explicit_args"] = chunk.tool_args

    def _end(self, chunk) -> Optional[ToolCall]:
        call_id = chunk.tool_call_id
        entry = None
        if call_id and call_id in self._pending:
            entry = self._pending.pop(call_id)
        elif self._pending:
            # provider 未回填 id：取最早未完成的一条
            first_key = next(iter(self._pending))
            call_id = first_key
            entry = self._pending.pop(first_key)
        else:
            entry = {"name": None, "args_str": [], "explicit_args": None, "order": self._seq}
            self._seq += 1

        name = chunk.tool_name or entry.get("name") or ""
        if chunk.tool_args is not None:
            raw_args = chunk.tool_args
        elif entry.get("explicit_args") is not None:
            raw_args = entry["explicit_args"]
        else:
            joined = "".join(entry.get("args_str") or [])
            raw_args = joined if joined.strip() else None

        resolved_id = call_id or f"call_{entry.get('order', 0)}"
        call = normalize_tool_call(name, raw_args, tool_call_id=resolved_id, index=entry.get("order") or 0)
        return self._register(call)

    def _finalize_pending(self) -> None:
        """流结束时仍有未闭合的工具调用：按已累积的原始串登记（标记 malformed 由解析层判断）。"""
        for call_id, entry in list(self._pending.items()):
            joined = "".join(entry.get("args_str") or [])
            raw = entry.get("explicit_args") if entry.get("explicit_args") is not None else (
                joined if joined.strip() else None
            )
            call = normalize_tool_call(
                entry.get("name") or "", raw, tool_call_id=call_id, index=entry.get("order") or 0
            )
            self._register(call)
        self._pending.clear()

    def _register(self, call: ToolCall) -> Optional[ToolCall]:
        if call.id in self._seen_ids:
            # §2.3 重复工具调用 ID：去重
            return None
        self._seen_ids.add(call.id)
        self.tool_calls.append(call)
        self.ordered_events.append(("tool_call", call))
        if call.malformed:
            self.malformed.append(call)
        return call

    # -- 结果 -------------------------------------------------------------

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    def result(self) -> dict:
        return {
            "text": self.text,
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "malformed": [c.to_dict() for c in self.malformed],
            "errors": list(self.errors),
            "incomplete": bool(self.incomplete),
            "done": bool(self.done),
            "usage": self.usage.to_dict(),
        }


def collect_stream(stream: Iterator, parser: Optional[ToolCallParser] = None) -> StreamCollector:
    """便捷函数：消费完整个流，返回收集器。"""
    collector = StreamCollector(parser=parser)
    for chunk in stream:
        collector.feed(chunk)
    return collector


def stream_with_deadline(client, messages, tools, sampling, timeout, deadline_s: float):
    """在总时长上限内消费流；超时后停止拉取并记录（§7.5 超时处理）。

    返回 `(collector, timed_out)`。超时是显式结果，不抛异常。
    """
    collector = StreamCollector()
    start = time.monotonic()
    timed_out = False
    stream = client.stream_chat(messages=messages, tools=tools, sampling=sampling, timeout=timeout)
    for chunk in stream:
        collector.feed(chunk)
        if deadline_s is not None and (time.monotonic() - start) > deadline_s:
            timed_out = True
            break
    else:
        return collector, False
    # 超时：把已接收部分标记为 incomplete，由上层进入 finalize
    collector.incomplete = True
    return collector, timed_out
