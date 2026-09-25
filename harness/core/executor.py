"""Executor：工具执行（构建规范 §5.4）。

实验模式下用 `DryRunExecutor` 只记录不真正执行；`LocalExecutor` 同样默认
`dry_run=True`，只有在显式允许的根目录内且经过审批的动作才会落到磁盘。
硬编码的外部命令执行**不提供**（安全默认），只做记录。
"""

from __future__ import annotations

import json
import os
from typing import Optional

from core.types import ToolCall, ToolResult


class DryRunExecutor:
    """只记录，不产生任何副作用（实验默认）。"""

    def __init__(self, log: Optional[list] = None):
        self.dry_run = True
        self.log = log if log is not None else []

    def execute(self, tool_call: ToolCall) -> ToolResult:
        record = {"tool": tool_call.name, "args": tool_call.args, "dry_run": True}
        self.log.append(record)
        return ToolResult(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            ok=True,
            output=f"[dry-run] would execute {tool_call.name} with args "
            + json.dumps(tool_call.args, ensure_ascii=False, sort_keys=True),
            executed=False,
            dry_run=True,
        )


class LocalExecutor:
    """受限本地执行器。

    - `execute_shell`：**始终** dry-run（不真正执行外部命令，避免实验污染主机）；
    - `read_file`：只允许读取 `allowed_roots` 内的文件；
    - `write_file`：默认 dry-run；
    - `http_request`：默认 dry-run（实验不发送真实网络请求）。
    """

    def __init__(self, allowed_roots: Optional[list] = None, dry_run: bool = True, base_dir: str = "."):
        self.dry_run = dry_run
        self.base_dir = os.path.abspath(base_dir)
        self.allowed_roots = [os.path.abspath(os.path.join(self.base_dir, r)) for r in (allowed_roots or ["."])]
        self.log: list = []
        self.fallback = DryRunExecutor()

    def _within_allowed(self, path: str) -> bool:
        candidate = os.path.normcase(os.path.abspath(os.path.join(self.base_dir, path)))
        for root in self.allowed_roots:
            root_norm = os.path.normcase(root)
            if candidate == root_norm or candidate.startswith(root_norm + os.sep):
                return True
        return False

    def execute(self, tool_call: ToolCall) -> ToolResult:
        handler = {
            "read_file": self._read_file,
            "write_file": self._write_file,
        }.get(tool_call.name)
        if handler is None:
            return self.fallback.execute(tool_call)
        return handler(tool_call)

    def _read_file(self, tool_call: ToolCall) -> ToolResult:
        path = tool_call.args.get("path", "")
        if not self._within_allowed(path):
            return ToolResult(
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                ok=False,
                error=f"path outside allowed roots: {path!r}",
                executed=False,
                dry_run=self.dry_run,
            )
        full = os.path.join(self.base_dir, path)
        try:
            with open(full, "r", encoding=tool_call.args.get("encoding") or "utf-8") as handle:
                content = handle.read()
        except OSError as exc:
            return ToolResult(
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                ok=False,
                error=str(exc),
                executed=False,
                dry_run=self.dry_run,
            )
        self.log.append({"tool": tool_call.name, "path": path, "ok": True})
        return ToolResult(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            ok=True,
            output=content,
            executed=True,
            dry_run=self.dry_run,
        )

    def _write_file(self, tool_call: ToolCall) -> ToolResult:
        path = tool_call.args.get("path", "")
        if self.dry_run or not self._within_allowed(path):
            return self.fallback.execute(tool_call)
        full = os.path.join(self.base_dir, path)
        try:
            os.makedirs(os.path.dirname(full) or self.base_dir, exist_ok=True)
            mode = "a" if tool_call.args.get("mode") == "append" else "w"
            with open(full, mode, encoding="utf-8") as handle:
                handle.write(tool_call.args.get("content", ""))
        except OSError as exc:
            return ToolResult(
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                ok=False,
                error=str(exc),
                executed=False,
                dry_run=False,
            )
        self.log.append({"tool": tool_call.name, "path": path, "ok": True})
        return ToolResult(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            ok=True,
            output=f"wrote {len(tool_call.args.get('content', ''))} chars to {path}",
            executed=True,
            dry_run=False,
        )
