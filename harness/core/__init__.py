"""core：harness 的机制层。

边界铁律（构建规范 §1）：
- `core/` **不依赖** `llm/`。所有 LLM 调用经由调用方注入的 `LLMClient` 抽象
  （鸭子类型：只要求有 `stream_chat` / 可选 `complete`）。
- `core/` 内部模块之间只通过 `core.types` 里定义的数据结构通信，不用全局状态。
"""
