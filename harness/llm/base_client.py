"""LLM 客户端抽象层（构建规范 §2.1）。

provider 差异（工具调用位置、流式参数增量、结束标记、系统提示位置）全部在本层
之下的适配器里消化；`core/` 与 `experiments/` 只看到统一的 `stream_chat` /
`complete` 接口与 `StreamChunk` 事件。
"""

from __future__ import annotations

from typing import Iterator, Optional

from core.types import SamplingConfig, StreamChunk, TokenUsage


class LLMError(RuntimeError):
    """provider 侧错误（网络、限流、超时）。"""


class LLMClient:
    """统一接口。子类必须实现 `stream_chat`。"""

    provider: str = "abstract"

    def stream_chat(
        self,
        messages: list,
        tools: list,
        sampling: SamplingConfig,
        timeout: float = 60.0,
    ) -> Iterator[StreamChunk]:
        raise NotImplementedError

    def complete(
        self,
        system: str,
        prompt: str,
        sampling: SamplingConfig,
        timeout: float = 20.0,
    ) -> tuple:
        """单轮补全（审核 Agent 用）。返回 `(text, TokenUsage)`。

        默认实现用一个“无 history”的 messages 走 stream_chat，保证审核调用
        与主调用共享同一套 provider 适配与统计。
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        text_parts: list = []
        usage = TokenUsage()
        for chunk in self.stream_chat(messages=messages, tools=[], sampling=sampling, timeout=timeout):
            if chunk.kind == "text" and chunk.text:
                text_parts.append(chunk.text)
            elif chunk.kind == "usage" or (chunk.kind == "done" and chunk.usage):
                if chunk.usage:
                    usage.add(chunk.usage)
            elif chunk.kind == "error":
                raise LLMError(chunk.error or "stream error")
        return "".join(text_parts), usage


def estimate_tokens(text: str) -> int:
    """粗略 token 估算（离线 FakeClient 与成本核算用，不用于真实计费）。"""
    if not text:
        return 0
    return max(1, len(text) // 4)


class RetryingClient:
    """给任意 LLMClient 加重试 + 指数退避（§11.6）。

    `sleep` 可注入，测试中传 `lambda _s: None` 保证确定性。
    """

    def __init__(self, inner: LLMClient, max_retries: int = 3, base_delay: float = 0.5, sleep=None):
        import time as _time

        self.inner = inner
        self.provider = getattr(inner, "provider", "unknown")
        self.max_retries = max_retries
        self.base_delay = base_delay
        self._sleep = sleep or _time.sleep
        self.attempts = 0

    def stream_chat(self, messages, tools, sampling, timeout=60.0) -> Iterator[StreamChunk]:
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            self.attempts += 1
            try:
                emitted = False
                for chunk in self.inner.stream_chat(
                    messages=messages, tools=tools, sampling=sampling, timeout=timeout
                ):
                    if chunk.kind == "error":
                        raise LLMError(chunk.error or "stream error")
                    emitted = True
                    yield chunk
                if emitted:
                    return
                raise LLMError("empty stream")
            except Exception as exc:  # noqa: BLE001 - 重试后再决定是否上报
                last_error = exc
                if attempt < self.max_retries - 1:
                    self._sleep(self.base_delay * (2 ** attempt))
        yield StreamChunk(kind="error", error=f"{type(last_error).__name__}: {last_error}")

    def complete(self, system, prompt, sampling, timeout=20.0):
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            self.attempts += 1
            try:
                return self.inner.complete(system=system, prompt=prompt, sampling=sampling, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < self.max_retries - 1:
                    self._sleep(self.base_delay * (2 ** attempt))
        raise LLMError(f"{type(last_error).__name__}: {last_error}")
