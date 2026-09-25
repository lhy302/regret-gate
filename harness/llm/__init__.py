"""llm：provider 适配层（构建规范 §2）。

`core/` 与 `experiments/` 只依赖本包暴露的抽象（`LLMClient` / `StreamChunk` /
`SamplingConfig`），不直接依赖任何 provider SDK。
"""

from llm.base_client import LLMClient, LLMError, RetryingClient, estimate_tokens  # noqa: F401
from llm.fake_client import FakeClient  # noqa: F401

__all__ = ["LLMClient", "LLMError", "RetryingClient", "estimate_tokens", "FakeClient", "build_client"]


def build_client(provider: str, **kwargs):
    """按配置构造客户端。离线默认走 FakeClient，不需要任何凭据。"""
    provider = (provider or "fake").lower()
    if provider == "fake":
        return FakeClient(**{k: v for k, v in kwargs.items() if k in _FAKE_KEYS})
    if provider == "openai":
        from llm.openai_client import OpenAIClient

        return OpenAIClient(
            api_key=kwargs.get("api_key", ""),
            model=kwargs.get("model", "gpt-4o-2024-08-06"),
            base_url=kwargs.get("base_url", "https://api.openai.com/v1"),
            transport=kwargs.get("transport"),
        )
    if provider == "anthropic":
        from llm.anthropic_client import AnthropicClient

        return AnthropicClient(
            api_key=kwargs.get("api_key", ""),
            model=kwargs.get("model", "claude-3-5-sonnet-20241022"),
            base_url=kwargs.get("base_url", "https://api.anthropic.com/v1"),
            transport=kwargs.get("transport"),
        )
    raise ValueError(f"unknown provider: {provider!r}")


_FAKE_KEYS = {
    "script",
    "category_of",
    "tail_audit_fixes",
    "external_dangerous_tool",
    "tail_audit_verdict",
    "audit_raw_override",
    "sleep_per_chunk",
}
