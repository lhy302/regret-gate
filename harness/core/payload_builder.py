"""PayloadBuilder：构造下一次请求的 messages（构建规范 §9）。

四条铁律（§9.3 + §15.1）：
1. **不把 `base_text` 放入 messages**——只放 `finalized_text`；
2. **不把修订历史放入 messages**——修订历史只存在于审计日志；
3. **不把 `tail_audit` 提示留在 messages 中**——注入后响应处理完即移除；
4. **不把 `PendingActions` 的内部状态放入 messages**。

另外必须计算并记录 `cache_break_point`：当前 payload 与上一回合 payload 最早不同的
字符位置（§9.4），用于成本分析。
"""

from __future__ import annotations

import json
from typing import Optional

from core.types import TokenUsage

SYSTEM_PROMPT_BASE = (
    "你是一个严谨的工程助手。请按用户要求直接产出最终内容。\n"
    "当你需要读写文件、执行命令或发请求时，调用提供的工具。\n"
    "除工具调用外，你的文本输出就是交付给用户的最终内容。"
)

PROMPT_APPENDIX_WEAK = "如果你在生成过程中发现之前写的内容有误，可以调用 draft.commit_revision 登记修改。"

PROMPT_APPENDIX_STRONG = """你具备在生成过程中回头修改已写内容的能力。请遵守以下规则：
1. 每写完一个函数、一个段落、一个模块，回头检查已写内容是否有语法、命名、接口、逻辑错误；
2. 发现错误时，立即调用 draft.commit_revision 登记修改，不要等到最后；
3. 修改时使用最小的 target_text 定位，不要重写整段；
4. 不确定是否错误时，宁可登记修改也不要放过。"""

# 尾部自审提示的识别标记（§9.3 铁律 3：注入后响应处理完即移除）
TAIL_AUDIT_MARKERS = ("[尾部强制自审]",)
# 尾部自审窗口回复的识别标记（同样不属于用户可见内容）
TAIL_AUDIT_RESPONSE_MARKERS = ("（无新增修订）", "已通读最终输出，未发现需要登记修改的问题。")


def build_system_prompt(prompt_condition: str, base: Optional[str] = None) -> str:
    """`prompt_condition` **只**影响 system_prompt 的追加部分（§11.3 / §15.3）。

    none：无追加；weak：追加一句；strong：追加一段。
    基础部分在三组之间逐字节相同，只允许追加。
    """
    base_text = SYSTEM_PROMPT_BASE if base is None else base
    if prompt_condition == "none":
        return base_text
    if prompt_condition == "weak":
        return f"{base_text}\n\n{PROMPT_APPENDIX_WEAK}"
    if prompt_condition == "strong":
        return f"{base_text}\n\n{PROMPT_APPENDIX_STRONG}"
    raise ValueError(f"unknown prompt_condition: {prompt_condition!r}")


def extract_appendix(system_prompt: str, base: Optional[str] = None) -> str:
    """取 system_prompt 相对基础部分追加的内容（测试用：验证只允许追加）。"""
    base_text = SYSTEM_PROMPT_BASE if base is None else base
    if system_prompt == base_text:
        return ""
    prefix = base_text + "\n\n"
    if system_prompt.startswith(prefix):
        return system_prompt[len(prefix) :]
    return ""


def _canonical(messages: list) -> str:
    return json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def first_difference(a: str, b: str) -> int:
    limit = min(len(a), len(b))
    for i in range(limit):
        if a[i] != b[i]:
            return i
    if len(a) == len(b):
        return len(a)
    return limit


class PayloadBuilder:
    def __init__(self, system_prompt: str = SYSTEM_PROMPT_BASE, tools: Optional[list] = None):
        self.system_prompt = system_prompt
        self.tools = list(tools or [])
        self.previous_payload: Optional[list] = None
        self.last_break_point: int = 0
        self.last_payload_len: int = 0
        self.usage = TokenUsage()

    # -- 构造 -------------------------------------------------------------

    def build(
        self,
        history: list,
        user_input: Optional[str] = None,
        finalized_text: Optional[str] = None,
        tool_results: Optional[list] = None,
    ) -> list:
        """构造一次请求的 payload.messages。

        - `history`：已完成回合的消息（**只含 assistant 的 finalized_text**，
          绝不接受 base_text 或 tail_audit 提示；本方法会主动过滤违规字段）；
        - `user_input`：新回合的用户输入（追加到尾部）；
        - `finalized_text`：本回合已定稿的输出（作为 assistant 消息）；
        - `tool_results`：本回合已批准操作的执行结果（作为 `role: tool` 消息）。

        返回的 messages 中：`system` 只有第一条；`base_text`、修订历史、
        `tail_audit` 提示、pending 内部态一律不出现。
        """
        messages: list = [{"role": "system", "content": self.system_prompt}]
        previous_payload = self.previous_payload
        messages.extend(self._sanitize_history(history))
        if user_input is not None:
            messages.append({"role": "user", "content": user_input})
        if finalized_text is not None:
            messages.append({"role": "assistant", "content": finalized_text})
        for result in tool_results or []:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": getattr(result, "tool_call_id", None),
                    "name": getattr(result, "tool_name", None),
                    "content": getattr(result, "output", "") or getattr(result, "error", "") or "",
                }
            )
        self.previous_payload = messages
        self.compute_cache_break_point(messages, previous=previous_payload)
        return messages

    @staticmethod
    def _is_tail_audit_content(content) -> bool:
        if not isinstance(content, str):
            return False
        return any(marker in content for marker in TAIL_AUDIT_MARKERS) or any(
            marker in content for marker in TAIL_AUDIT_RESPONSE_MARKERS
        )

    @classmethod
    def _sanitize_history(cls, history: list) -> list:
        """把历史消息清洗成可发送的形态（剥 system / 自审痕迹 / 审计专用字段）。"""
        clean: list = []
        for item in history or []:
            sanitized = cls._sanitize_history_item(item)
            if sanitized is not None:
                clean.append(sanitized)
        return clean

    @classmethod
    def _sanitize_history_item(cls, item: dict) -> Optional[dict]:
        """过滤审计专用内容：base_text / 修订历史 / tail_audit 提示 / pending 内部态。"""
        if not isinstance(item, dict):
            return None
        role = item.get("role")
        if role == "system":
            # system 只允许由 PayloadBuilder 通过 system_prompt 提供
            return None
        content = item.get("content")
        if content is None:
            content = ""
        if cls._is_tail_audit_content(content):
            # 铁律 3：尾部自审提示/回复注入后处理完即移除，绝不进入下一次 payload。
            # 无论它以 user 消息还是被并入 assistant 内容的形式出现，这里统一剥离。
            return None
        clean = {"role": role, "content": content}
        if item.get("tool_calls"):
            clean["tool_calls"] = item["tool_calls"]
        if item.get("tool_call_id"):
            clean["tool_call_id"] = item["tool_call_id"]
        return clean

    # -- 缓存破坏点（§9.4） ----------------------------------------------

    def compute_cache_break_point(self, payload: list, previous: Optional[list] = None) -> int:
        """当前 payload 与上一次 payload 最早不同的字符位置。"""
        previous = self.previous_payload if previous is None else previous
        if previous is None:
            self.last_break_point = 0
            return 0
        current_text = _canonical(payload)
        previous_text = _canonical(previous)
        self.last_break_point = first_difference(previous_text, current_text)
        self.last_payload_len = len(current_text)
        return self.last_break_point

    def cache_break_ratio(self) -> float:
        if not self.last_payload_len:
            return 0.0
        return self.last_break_point / self.last_payload_len

    def snapshot(self) -> dict:
        return {
            "system_prompt": self.system_prompt,
            "payload_len": self.last_payload_len,
            "cache_break_point": self.last_break_point,
            "cache_break_ratio": self.cache_break_ratio(),
            "tool_count": len(self.tools),
        }
