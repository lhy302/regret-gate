"""TailAudit：尾部强制自审（构建规范 §7）。

关键约束：
- **每回合必注入**（§7.1），不是拦截后才注入（这是与 v4.1 的核心差异，v0.2 §3.3）；
- 注入为 `role: user` 消息（§7.3）；不允许用 `role: system`；
- 每回合**只注入一次**（§7.4 / §7.6 / 铁律 5）；
- 注入后再次进入生成循环最多 3 次，防死循环（§7.6）；
- 超时（默认 30s）记 `tail_audit_timeout` 后进入 finalize（§7.5）；
- 响应不得混入 `base_text`（§7.6）——本类只把响应拆成 text / tool_calls 返回给调用方，
  由上层决定去向。
"""

from __future__ import annotations

import time
from typing import Optional

from core.stream_collector import StreamCollector
from core.types import TokenUsage

TAIL_AUDIT_PROMPT = """[尾部强制自审]
本回合输出已完成。请通读最终输出字符串，特别检查：
1. 尾部是否包含将被执行的命令、写操作、外部调用；
2. 这些操作的参数、路径、作用域是否正确；
3. 若发现错误，调用 draft.commit_revision 登记修改；
4. 若无误，以纯文本结束本回合。"""


class TailAudit:
    """注入器 + 一次性约束的状态机。"""

    def __init__(self, timeout: float = 30.0, max_extra_loops: int = 3, prompt: Optional[str] = None):
        self.timeout = timeout
        self.max_extra_loops = max_extra_loops
        self.prompt = prompt or TAIL_AUDIT_PROMPT
        self.turn = 0
        self.injected = False
        self.inject_count = 0
        self.extra_loops = 0
        self.events: list = []

    # -- 回合生命周期 -----------------------------------------------------

    def start_turn(self, turn: int) -> None:
        self.turn = turn
        self.injected = False
        self.inject_count = 0
        self.extra_loops = 0

    def should_inject(self) -> bool:
        """每回合必注入，且只注入一次。"""
        return not self.injected

    def append_prompt(self, messages: list) -> list:
        """把自审提示作为 `role: user` 追加，返回新的 messages 列表（不修改入参）。"""
        new_messages = list(messages)
        new_messages.append({"role": "user", "content": self.prompt})
        return new_messages

    def mark_injected(self) -> bool:
        if self.injected:
            return False
        self.injected = True
        self.inject_count += 1
        self.events.append({"turn": self.turn, "event": "injected", "at": time.time()})
        return True

    def allow_extra_loop(self) -> bool:
        """注入后最多再生成 3 次（§7.6）。"""
        if self.extra_loops >= self.max_extra_loops:
            return False
        self.extra_loops += 1
        return True

    # -- 主流程 -----------------------------------------------------------

    def run(
        self,
        client,
        messages: list,
        tools: list,
        sampling,
        request_timeout: float,
    ) -> dict:
        """执行一次尾部自审调用，返回结构化结果。

        返回：
        ```
        {
          "injected": bool,
          "timed_out": bool,
          "text": str,                 # 自审响应的纯文本（不得混入 base_text）
          "tool_calls": list[ToolCall],
          "responses": list[dict],     # 每次额外循环的响应
          "usage": TokenUsage,
          "messages_used": list,       # 含注入提示的 messages（供审计）
          "loop_limit_hit": bool,
        }
        ```
        """
        result = {
            "injected": False,
            "timed_out": False,
            "text": "",
            "tool_calls": [],
            "responses": [],
            "usage": TokenUsage(),
            "messages_used": None,
            "loop_limit_hit": False,
        }
        if not self.should_inject():
            result["loop_limit_hit"] = True
            return result

        messages_with_prompt = self.append_prompt(messages)
        self.mark_injected()
        result["injected"] = True
        result["messages_used"] = messages_with_prompt

        text_parts: list = []
        while True:
            if not self.allow_extra_loop():
                result["loop_limit_hit"] = True
                break
            collector = StreamCollector()
            start = time.monotonic()
            timed_out = False
            stream = client.stream_chat(
                messages=messages_with_prompt,
                tools=tools,
                sampling=sampling,
                timeout=request_timeout,
            )
            for chunk in stream:
                collector.feed(chunk)
                if (time.monotonic() - start) > self.timeout:
                    timed_out = True
                    collector.incomplete = True
                    break
            result["usage"].add(collector.usage)
            result["responses"].append(collector.result())
            if collector.text:
                text_parts.append(collector.text)
            result["tool_calls"].extend(collector.tool_calls)
            if timed_out:
                result["timed_out"] = True
                break
            # 有修订登记 → 继续一次（让模型在修订后可以重新收尾）；否则结束
            if not any(c.name == "draft.commit_revision" for c in collector.tool_calls):
                break

        result["text"] = "".join(text_parts)
        return result

    def snapshot(self) -> dict:
        return {
            "turn": self.turn,
            "injected": self.injected,
            "inject_count": self.inject_count,
            "extra_loops": self.extra_loops,
            "events": list(self.events),
        }
