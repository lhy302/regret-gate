"""AuditLogger：不可变审计日志（构建规范 §10）。

- 每行一个 JSON 对象（JSONL）；
- 字段固定为 `ts / turn / event / run_id / group / task_id / data`；
- **不可变**：写入后不修改。如需修正，追加新事件，不覆盖旧事件；
- 必须保留的原始数据：`base_text`、`finalized_text`、`revisions`（含 rejected）、
  `pending_actions`（含决议）、`cache_break_point`、`token_usage`。
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

EVENT_TYPES = (
    "turn_start",
    "stream_chunk",
    "tool_call_parsed",
    "risk_decision",
    "pending_added",
    "revision_pushed",
    "tail_audit_injected",
    "tail_audit_response",
    "finalize_done",
    "external_audit_request",
    "external_audit_response",
    "pending_resolved",
    "tool_executed",
    "turn_end",
    "error",
)


class AuditLogger:
    """追加写 JSONL。`fsync=False` 以便测试中使用内存缓冲。"""

    def __init__(
        self,
        path: Optional[str] = None,
        run_id: str = "run",
        group: str = "A",
        task_id: str = "task",
        clock=None,
        stream=None,
    ):
        self.path = path
        self.run_id = run_id
        self.group = group
        self.task_id = task_id
        self._clock = clock or time.time
        self._stream = stream
        self.records: list = []
        self.closed = False
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            # 追加模式：绝不覆盖已有审计
            self._stream = open(path, "a", encoding="utf-8", newline="\n")
        elif self._stream is None:
            # 无路径也无流：只留内存记录（测试 / metric-only 模式）
            self._stream = _NullStream()

    # -- 写入 -------------------------------------------------------------

    def log(self, event: str, turn: int = 0, data: Optional[dict] = None) -> dict:
        if self.closed:
            raise RuntimeError("audit log is closed; append-only writer cannot be reopened")
        if event not in EVENT_TYPES:
            raise ValueError(f"unknown audit event type: {event!r}")
        record = {
            "ts": float(self._clock()),
            "turn": int(turn),
            "event": event,
            "run_id": self.run_id,
            "group": self.group,
            "task_id": self.task_id,
            "data": data or {},
        }
        line = json.dumps(_jsonable(record), ensure_ascii=False, sort_keys=True)
        self._stream.write(line + "\n")
        try:
            self._stream.flush()
        except (ValueError, OSError):
            pass
        self.records.append(record)
        return record

    # -- 读取 -------------------------------------------------------------

    def events(self, event: Optional[str] = None) -> list:
        if event is None:
            return list(self.records)
        return [r for r in self.records if r["event"] == event]

    def timeline(self) -> list:
        """还原完整时间线（scenario 1 验收用）。"""
        return [
            {
                "seq": idx,
                "turn": r["turn"],
                "event": r["event"],
                "data": r["data"],
            }
            for idx, r in enumerate(self.records)
        ]

    def close(self) -> None:
        if self._stream is not None and hasattr(self._stream, "close"):
            self._stream.close()
        self.closed = True

    def __enter__(self) -> "AuditLogger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class NullAuditLogger(AuditLogger):
    """不落盘、只留内存记录的审计器（测试与 metric-only 模式）。"""

    def __init__(self, **kwargs):
        kwargs.pop("path", None)
        super().__init__(path=None, stream=_NullStream(), **kwargs)


class _NullStream:
    def write(self, _data: str) -> int:
        return len(_data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def _jsonable(obj):
    from core.types import to_jsonable

    return to_jsonable(obj)