"""Anthropic 流式客户端（构建规范 §2.2）。

差异处理：
- 系统提示是独立的 `system` 参数，必须从 messages 中拆出来；
- 工具调用是 `content` 中的 `tool_use` block，参数按 `input_json_delta` 增量到达；
- 结束标记 `stop_reason` / `message_stop` → 归一化为 `done`；
- 工具 schema 用 `input_schema` 而不是 `parameters`。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Iterator, Optional

from core.types import SamplingConfig, StreamChunk, TokenUsage
from llm.base_client import LLMClient, LLMError


def _http_transport(url: str, headers: dict, body: bytes, timeout: float):
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:  # pragma: no cover - 需要真实网络
        return exc.code, exc.read(), dict(exc.headers or {})
    except urllib.error.URLError as exc:  # pragma: no cover - 需要真实网络
        raise LLMError(f"transport error: {exc}") from exc


def _split_response(result) -> tuple:
    """兼容三元组（新）与二元组（旧的自定义 transport）。"""
    if len(result) == 3:
        return result[0], result[1], result[2] or {}
    return result[0], result[1], {}


def request_id_from(headers: dict) -> Optional[str]:
    """从响应头提取 provider 请求 ID（§11.5 可追溯）。"""
    for key in ("request-id", "x-request-id", "anthropic-request-id"):
        for header, value in (headers or {}).items():
            if header.lower() == key and value:
                return str(value)
    return None


def iter_sse_lines(payload: bytes) -> Iterator[str]:
    text = payload.decode("utf-8", errors="replace")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("data:"):
            yield line[len("data:") :].strip()


def split_system(messages: list) -> tuple:
    """把 `messages[0].role == 'system'` 拆成独立 system 参数。"""
    system_parts = []
    rest = []
    for item in messages:
        if item.get("role") == "system":
            system_parts.append(item.get("content") or "")
        else:
            rest.append(item)
    return "\n\n".join(p for p in system_parts if p), rest


def convert_tools(tools: list) -> list:
    converted = []
    for tool in tools or []:
        function = tool.get("function", tool)
        converted.append(
            {
                "name": function.get("name"),
                "description": function.get("description", ""),
                "input_schema": function.get("parameters", {"type": "object", "properties": {}}),
            }
        )
    return converted


class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.anthropic.com/v1",
        transport=None,
        version: str = "2023-06-01",
        max_tokens: Optional[int] = None,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.transport = transport or _http_transport
        self.version = version
        self.max_tokens = max_tokens

    def build_body(self, messages: list, tools: list, sampling: SamplingConfig, stream: bool = True) -> dict:
        system, rest = split_system(messages)
        body = {
            "model": self.model,
            "messages": rest,
            "max_tokens": self.max_tokens or sampling.max_tokens,
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
            "stream": stream,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = convert_tools(tools)
        return body

    def stream_chat(self, messages, tools, sampling: SamplingConfig, timeout: float = 60.0):
        body = json.dumps(self.build_body(messages, tools, sampling)).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.version,
        }
        try:
            status, payload = self.transport(f"{self.base_url}/messages", headers, body, timeout)
        except LLMError as exc:
            yield StreamChunk(kind="error", error=str(exc))
            return
        if status != 200:
            yield StreamChunk(kind="error", error=f"http {status}: {payload[:400].decode('utf-8', 'replace')}")
            return
        yield from self.parse_stream(payload)

    @staticmethod
    def parse_stream(payload: bytes) -> Iterator[StreamChunk]:
        open_calls: dict = {}
        usage = TokenUsage()
        for data in iter_sse_lines(payload):
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "message_start":
                usage.input_tokens = int(((event.get("message") or {}).get("usage") or {}).get("input_tokens", 0))
                continue
            if kind == "message_delta":
                usage.output_tokens = int((event.get("usage") or {}).get("output_tokens", usage.output_tokens))
                continue
            if kind == "content_block_start":
                block = event.get("content_block") or {}
                if block.get("type") == "tool_use":
                    call_id = block.get("id") or f"call_{event.get('index', 0)}"
                    open_calls[event.get("index", 0)] = {"id": call_id, "name": block.get("name")}
                    yield StreamChunk(
                        kind="tool_call_start", tool_call_id=call_id, tool_name=block.get("name")
                    )
                continue
            if kind == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    yield StreamChunk(kind="text", text=delta["text"])
                if delta.get("type") == "input_json_delta":
                    call = open_calls.get(event.get("index", 0))
                    if call:
                        yield StreamChunk(
                            kind="tool_call_delta",
                            tool_call_id=call["id"],
                            tool_args_delta=delta.get("partial_json") or "",
                        )
                continue
            if kind == "content_block_stop":
                call = open_calls.pop(event.get("index", 0), None)
                if call:
                    yield StreamChunk(
                        kind="tool_call_end", tool_call_id=call["id"], tool_name=call["name"], tool_args=None
                    )
                continue
            if kind == "message_stop":
                break
        yield StreamChunk(kind="done", usage=usage)

    def complete(self, system, prompt, sampling, timeout: float = 20.0):
        messages = [{"role": "user", "content": prompt}]
        body = json.dumps(self.build_body(messages, [], sampling, stream=False)).encode("utf-8")
        if system:
            body_obj = json.loads(body)
            body_obj["system"] = system
            body = json.dumps(body_obj).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.version,
        }
        status, payload = self.transport(f"{self.base_url}/messages", headers, body, timeout)
        if status != 200:
            raise LLMError(f"http {status}: {payload[:400].decode('utf-8', 'replace')}")
        event = json.loads(payload.decode("utf-8", "replace"))
        text = ""
        for block in event.get("content", []) or []:
            if block.get("type") == "text":
                text += block.get("text") or ""
        usage = event.get("usage") or {}
        return text, TokenUsage(int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)))
