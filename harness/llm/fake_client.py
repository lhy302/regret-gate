"""FakeClient：完全确定性的离线 LLM 客户端（构建规范 §2.4）。

两种工作模式：

1. **script 模式**：`FakeClient(script=[...])`，逐次调用按脚本吐 chunk，
   用于 `core/` 各模块的单元测试。
2. **generate 模式**：按任务类别确定性地生成长输出（>300 行代码 / >2000 字文本），
   并在尾部自审阶段按配置修正或登记错误。用于离线跑完 A–H 组。

确定性保证：
- 所有内容由任务 id 哈希派生（见 `llm/generators.py`），同 seed 逐字节一致；
- token 只做估算（`estimate_tokens`），不引入任何真实计时或随机源；
- 流式分块数量固定；
- 尾部自审窗口只服务一次（§7.4），避免重复登记同一修订。
"""

from __future__ import annotations

import json
from typing import Iterator, Optional

from core.types import SamplingConfig, StreamChunk, TokenUsage
from llm.base_client import LLMClient, estimate_tokens
from llm.generators import (
    chunkify,
    faulty_code_block,
    fixed_code_block,
    generate_output,
    mixed_path,
    short_command_target,
    text_revision_pair,
)

TAIL_AUDIT_MARKER = "[尾部强制自审]"


class FakeClient(LLMClient):
    """离线确定性客户端。

    参数：
    - `script`：script 模式事件列表；每次 `stream_chat` 调用消费脚本里的一组事件。
    - `tail_audit_fixes`：尾部自审是否登记修订（False 用于对照“模型不响应尾部提示”）。
    - `external_dangerous_tool`：主回合是否抛出危险工具调用（测 RiskRouter 链路）。
    - `tail_audit_verdict`：`complete()`（审核 Agent 角色）返回的 verdict。
    - `audit_raw_override`：审核响应原文覆盖（测 JSON 解析失败路径）。
    """

    provider = "fake"

    def __init__(
        self,
        script: Optional[list] = None,
        category_of: Optional[dict] = None,
        tail_audit_fixes: bool = True,
        external_dangerous_tool: bool = True,
        tail_audit_verdict: str = "approve",
        audit_raw_override: Optional[str] = None,
        spontaneous_revision: bool = False,
        sleep_per_chunk: float = 0.0,
    ):
        self._script = list(script) if script is not None else None
        self.category_of = dict(category_of or {})
        self.tail_audit_fixes = tail_audit_fixes
        self.spontaneous_revision = spontaneous_revision
        self.external_dangerous_tool = external_dangerous_tool
        self.tail_audit_verdict = tail_audit_verdict
        self.audit_raw_override = audit_raw_override
        self.sleep_per_chunk = sleep_per_chunk
        self.calls: list = []
        self.task_id = "task"
        self.category = "long_code"
        self.core = "通用工具模块"
        self.dangerous_operations: list = []
        self._tail_served = False

    # -- 任务注入 ---------------------------------------------------------

    def set_task(self, task) -> None:
        """由运行器在每次 run 前调用，注入当前任务（不影响确定性）。

        客户端实例会被多次 run 复用，因此这里同时把调用计数与尾部窗口状态复位，
        避免跨 run 累计（`llm_calls` 与 `tail_audit_invocations` 必须按 run 计）。
        """
        self.task_id = getattr(task, "id", "task")
        self.category = getattr(task, "category", "long_code")
        metadata = getattr(task, "metadata", None) or {}
        self.core = metadata.get("core_concept") or "通用工具模块"
        self.dangerous_operations = list(getattr(task, "dangerous_operations", None) or [])
        self._tail_served = False
        self.calls = []

    # -- 主入口 -----------------------------------------------------------

    def stream_chat(self, messages, tools, sampling: SamplingConfig, timeout: float = 60.0):
        self.calls.append(
            {
                "model": getattr(sampling, "model", None),
                "temperature": getattr(sampling, "temperature", None),
                "top_p": getattr(sampling, "top_p", None),
                "seed": getattr(sampling, "seed", None),
                "message_count": len(messages),
                "tools": [t.get("function", {}).get("name") for t in tools],
                "last_role": messages[-1].get("role") if messages else None,
                "tail_audit": self.is_tail_audit_request(messages),
            }
        )
        if self._script is not None:
            yield from self._run_script(messages)
            return
        yield from self._run_generate(messages)

    # -- script 模式 ------------------------------------------------------

    def _run_script(self, messages) -> Iterator[StreamChunk]:
        events = self._script.pop(0) if self._script else [{"kind": "done"}]
        if isinstance(events, dict):
            events = [events]
        for event in events:
            yield StreamChunk(**event)
            if self.sleep_per_chunk:
                import time as _time

                _time.sleep(self.sleep_per_chunk)

    # -- generate 模式 ----------------------------------------------------

    @staticmethod
    def is_tail_audit_request(messages) -> bool:
        if not messages:
            return False
        last = messages[-1]
        content = last.get("content") or ""
        return last.get("role") == "user" and isinstance(content, str) and TAIL_AUDIT_MARKER in content

    def _run_generate(self, messages) -> Iterator[StreamChunk]:
        if not self.is_tail_audit_request(messages):
            # 非尾部自审请求 = 新回合的主请求：重置尾部一次性约束
            self._tail_served = False
            yield from self._main_response()
            return
        if self._tail_served:
            # 尾部自审窗口只给一次提示（§7.4）；第二次调用只以纯文本结束窗口
            text = "（无新增修订）"
            for piece in chunkify(text):
                yield StreamChunk(kind="text", text=piece)
            yield StreamChunk(
                kind="done", usage=TokenUsage(input_tokens=60, output_tokens=estimate_tokens(text))
            )
            return
        self._tail_served = True
        yield from self._tail_audit_response()

    def _main_response(self) -> Iterator[StreamChunk]:
        text = generate_output(
            self.task_id,
            self.category,
            core=self.core,
            operation=self._primary_operation() if self.category == "short_command" else None,
        )
        usage = TokenUsage(input_tokens=320, output_tokens=estimate_tokens(text))

        if self.spontaneous_revision and self.category in ("long_code", "long_text"):
            # 提示词模拟：模型在生成过程中主动登记修订（不依赖尾部自审）。
            # 这里登记的是“确认无需改动”的等价修订：作用域与机制链路被真实触发，
            # 但不改变最终文本，避免把“能力边界”与“尾部自审的修复效果”混在一起。
            yield from self._tool_call_chunks(self._spontaneous_revision_call())
            usage.output_tokens += 120

        if self.category in ("short_command", "mixed") and self.external_dangerous_tool:
            dangerous = self._dangerous_call()
            yield from self._tool_call_chunks(dangerous)
            usage.input_tokens += 32
            usage.output_tokens += estimate_tokens(json.dumps(dangerous["args"], ensure_ascii=False))

        if self.category == "mixed":
            yield from self._tool_call_chunks(self._write_call())

        for piece in chunkify(text):
            yield StreamChunk(kind="text", text=piece)
        yield StreamChunk(kind="done", usage=usage)

    @staticmethod
    def _tool_call_chunks(call: dict) -> Iterator[StreamChunk]:
        yield StreamChunk(kind="tool_call_start", tool_call_id=call["id"], tool_name=call["name"])
        payload = json.dumps(call["args"], ensure_ascii=False)
        for piece in chunkify(payload, 24):
            yield StreamChunk(kind="tool_call_delta", tool_call_id=call["id"], tool_args_delta=piece)
        yield StreamChunk(
            kind="tool_call_end",
            tool_call_id=call["id"],
            tool_name=call["name"],
            tool_args=call["args"],
        )

    def _dangerous_call(self) -> dict:
        if self.category == "short_command":
            command = f"{self._primary_operation()} {short_command_target(self.task_id)}"
        else:
            command = f"Get-Content {mixed_path(self.task_id)}"
        return {
            "id": f"call_shell_{self.task_id}",
            "name": "execute_shell",
            "args": {"command": command, "shell": "powershell"},
        }

    def _primary_operation(self) -> str:
        """取任务声明的高危操作作为实际发出的命令。

        离线模型必须发出**该任务真正标注的高危操作**，否则“实际发出的操作”和
        `dangerous_operations` 会对不上，误拦截率会被注入器的偏差污染。
        多词操作（如 `git reset --hard`）补一个合理参数使其可执行。
        """
        if not self.dangerous_operations:
            return "Remove-Item -Recurse -Force"
        declared = str(self.dangerous_operations[0]).strip()
        lowered = declared.lower()
        if lowered.startswith("git reset"):
            return "git reset --hard HEAD~1"
        if lowered.startswith("git push"):
            return "git push --force origin main"
        if lowered.startswith("remove-item"):
            return "Remove-Item -Recurse -Force"
        return declared

    def _write_call(self) -> dict:
        return {
            "id": f"call_write_{self.task_id}",
            "name": "write_file",
            "args": {"path": mixed_path(self.task_id), "content": "# generated module\n", "mode": "overwrite"},
        }

    def _tail_audit_response(self) -> Iterator[StreamChunk]:
        if self.tail_audit_fixes and self.category in ("long_code", "long_text"):
            revision = self._revision_call()
            yield from self._tool_call_chunks(revision)
            text = "已登记一处修订，用于修正上述缺陷。"
            for piece in chunkify(text):
                yield StreamChunk(kind="text", text=piece)
            yield StreamChunk(
                kind="done",
                usage=TokenUsage(
                    input_tokens=140, output_tokens=estimate_tokens(json.dumps(revision["args"], ensure_ascii=False) + text)
                ),
            )
            return
        text = "已通读最终输出，未发现需要登记修改的问题。"
        for piece in chunkify(text):
            yield StreamChunk(kind="text", text=piece)
        yield StreamChunk(
            kind="done", usage=TokenUsage(input_tokens=140, output_tokens=estimate_tokens(text))
        )

    def _revision_call(self) -> dict:
        if self.category == "long_text":
            target, payload = text_revision_pair(self.task_id)
            reason = "替换未完成的占位符段落，使章节内容完整"
        else:
            target = faulty_code_block(self.task_id)
            payload = fixed_code_block(self.task_id)
            reason = "修正顶层函数定义缺少冒号的语法错误"
        return {
            "id": f"call_rev_{self.task_id}",
            "name": "draft.commit_revision",
            "args": {"target_text": target, "op": "replace", "payload": payload, "reason": reason},
        }

    def _spontaneous_revision_call(self) -> dict:
        """自发生成期修订：登记一条有效但无实质改变的修订（登记本身即被测行为）。"""
        call = self._revision_call()
        args = dict(call["args"])
        args["payload"] = args["target_text"]
        args["reason"] = "生成过程中主动回头检查，确认该片段无需改动"
        call["args"] = args
        call["id"] = f"call_spont_{self.task_id}"
        return call

    # -- 审核 Agent 角色（complete） --------------------------------------

    def complete(self, system: str, prompt: str, sampling: SamplingConfig, timeout: float = 20.0):
        self.calls.append(
            {
                "role": "auditor",
                "model": getattr(sampling, "model", None),
                "temperature": getattr(sampling, "temperature", None),
                "message_count": 2,
                "system_prefix": system[:24],
            }
        )
        if self.audit_raw_override is not None:
            raw = self.audit_raw_override
        else:
            raw = json.dumps(
                {
                    "verdict": self.tail_audit_verdict,
                    "issues": []
                    if self.tail_audit_verdict == "approve"
                    else [
                        {
                            "severity": "high",
                            "description": "该操作会递归删除目标目录，作用域未确认。",
                            "suggestion": "先确认路径并改为显式文件清单。",
                        }
                    ],
                },
                ensure_ascii=False,
            )
        return raw, TokenUsage(input_tokens=estimate_tokens(prompt), output_tokens=estimate_tokens(raw))

    # -- 统计 -------------------------------------------------------------

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def reset(self) -> None:
        self.calls = []
