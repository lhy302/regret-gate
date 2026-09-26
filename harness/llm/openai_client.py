"""OpenAI 兼容流式客户端（构建规范 §2.2）。

差异处理：
- 工具调用在独立的 `tool_calls` 字段，参数按 `delta.tool_calls[].function.arguments` 增量到达；
- 结束标记 `finish_reason` → 归一化为 `done`；
- 系统提示是 `messages[0].role == "system"`，无需转换。

只依赖标准库（urllib），不引入第三方 SDK；`transport` 可注入以便离线单测。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Iterator, Optional

from core.types import SamplingConfig, StreamChunk, TokenUsage
from llm.base_client import LLMClient, LLMError


def _http_transport(url: str, headers: dict, body: bytes, timeout: float):
    """默认传输层：返回 `(status, body_bytes, response_headers)`。"""
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
    for key in ("x-request-id", "request-id", "x-amzn-requestid", "openai-request-id"):
        for header, value in (headers or {}).items():
            if header.lower() == key and value:
                return str(value)
    return None


def iter_sse_lines(payload: bytes) -> Iterator[str]:
    text = payload.decode("utf-8", errors="replace")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("data:"):
            continue
        yield line[len("data:") :].strip()


class OpenAIClient(LLMClient):
    provider = "openai"

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        transport=None,
        extra_headers: Optional[dict] = None,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.transport = transport or _http_transport
        self.extra_headers = dict(extra_headers or {})

    # -- 请求体 -----------------------------------------------------------

    def build_body(self, messages: list, tools: list, sampling: SamplingConfig, stream: bool = True) -> dict:
        body = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
            "max_tokens": sampling.max_tokens,
        }
        if sampling.seed is not None:
            body["seed"] = sampling.seed
        if tools:
            body["tools"] = tools
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    def stream_chat(self, messages, tools, sampling: SamplingConfig, timeout: float = 60.0):
        body = json.dumps(self.build_body(messages, tools, sampling)).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            **self.extra_headers,
        }
        try:
            status, payload, response_headers = _split_response(
                self.transport(f"{self.base_url}/chat/completions", headers, body, timeout)
            )
        except LLMError as exc:
            yield StreamChunk(kind="error", error=str(exc))
            return
        if status != 200:
            yield StreamChunk(kind="error", error=f"http {status}: {payload[:400].decode('utf-8', 'replace')}")
            return
        request_id = request_id_from(response_headers)
        for chunk in self.parse_stream(payload):
            if chunk.request_id is None:
                chunk.request_id = request_id
            yield chunk

    @staticmethod
    def parse_stream(payload: bytes, request_id: Optional[str] = None) -> Iterator[StreamChunk]:
        open_calls: dict = {}
        usage: Optional[TokenUsage] = None
        for data in iter_sse_lines(payload):
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if event.get("id") and request_id is None:
                request_id = str(event["id"])
            if event.get("usage"):
                usage = TokenUsage(
                    int(event["usage"].get("prompt_tokens", 0)),
                    int(event["usage"].get("completion_tokens", 0)),
                )
            for choice in event.get("choices", []) or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    yield StreamChunk(kind="text", text=delta["content"])
                for tool_delta in delta.get("tool_calls") or []:
                    index = tool_delta.get("index", 0)
                    call_id = tool_delta.get("id") or open_calls.get(index, {}).get("id") or f"call_{index}"
                    function = tool_delta.get("function") or {}
                    if index not in open_calls:
                        open_calls[index] = {"id": call_id, "name": function.get("name")}
                        yield StreamChunk(
                            kind="tool_call_start", tool_call_id=call_id, tool_name=function.get("name")
                        )
                    if function.get("arguments"):
                        yield StreamChunk(
                            kind="tool_call_delta",
                            tool_call_id=call_id,
                            tool_args_delta=function["arguments"],
                        )
                if choice.get("finish_reason"):
                    for index, call in open_calls.items():
                        yield StreamChunk(
                            kind="tool_call_end",
                            tool_call_id=call["id"],
                            tool_name=call["name"],
                            tool_args=None,
                        )
                    open_calls = {}
        yield StreamChunk(kind="done", usage=usage, request_id=request_id)

    def complete(self, system, prompt, sampling, timeout: float = 20.0):
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        body = json.dumps(self.build_body(messages, [], sampling, stream=False)).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            **self.extra_headers,
        }
        status, payload, response_headers = _split_response(
            self.transport(f"{self.base_url}/chat/completions", headers, body, timeout)
        )
        if status != 200:
            raise LLMError(f"http {status}: {payload[:400].decode('utf-8', 'replace')}")
        event = json.loads(payload.decode("utf-8", "replace"))
        text = ""
        for choice in event.get("choices", []) or []:
            text += (choice.get("message") or {}).get("content") or ""
        usage = event.get("usage") or {}
        self.last_request_id = request_id_from(response_headers) or event.get("id")
        return text, TokenUsage(
            int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        )
