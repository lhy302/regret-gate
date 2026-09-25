"""experiments：编排层（构建规范 §11/§13）。

边界：本包**不直连任何 provider SDK**，只通过 `llm/base_client` 的抽象接口，
并且只调用 `core/` 的公共接口。
"""
