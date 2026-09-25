"""PendingActions：被拦截 / 被缓冲的工具调用暂存与状态机（构建规范 §5）。

状态机（§5.2）：

```
pending → approved → executed → audited
       → rejected → audited
       → revised → pending（重新审核）
```

执行时机（§5.3）：
- `allow`    ：不进 PendingActions，立即执行；
- `buffer`   ：入栈，回合 finalize 后、尾部自审通过后执行；
- `intercept`：入栈，回合 finalize 后、尾部自审通过后、外部审核通过后执行。
"""

from __future__ import annotations

from typing import Optional

from core.types import (
    PENDING_APPROVED,
    PENDING_AUDITED,
    PENDING_EXECUTED,
    PENDING_PENDING,
    PENDING_REJECTED,
    PENDING_REVISED,
    RISK_INTERCEPT,
    PendingAction,
    RiskDecision,
    ToolCall,
    ToolResult,
)

_VALID_TRANSITIONS = {
    PENDING_PENDING: {PENDING_APPROVED, PENDING_REJECTED, PENDING_REVISED},
    PENDING_REVISED: {PENDING_APPROVED, PENDING_REJECTED, PENDING_REVISED},
    PENDING_APPROVED: {PENDING_EXECUTED, PENDING_REVISED},
    PENDING_EXECUTED: {PENDING_AUDITED},
    PENDING_REJECTED: {PENDING_AUDITED},
    PENDING_AUDITED: set(),
}


class PendingActions:
    def __init__(self, turn: int = 0):
        self.turn = turn
        self._items: list = []
        self._counter = 0

    # -- 写入 -------------------------------------------------------------

    def add(self, tool_call: ToolCall, decision: RiskDecision, step: int = 0) -> str:
        """登记一个待决操作，返回 pending_id。"""
        self._counter += 1
        pending_id = f"p{turn_label(self.turn)}_{self._counter}"
        item = PendingAction(
            id=pending_id,
            tool_call=tool_call,
            decision=decision,
            status=PENDING_PENDING,
        )
        item.resolution_history.append(
            {"status": PENDING_PENDING, "step": step, "level": decision.level, "reason": decision.reason}
        )
        self._items.append(item)
        return pending_id

    def resolve(
        self,
        pending_id: str,
        resolution: str,
        step: int = 0,
        audit_verdict: Optional[str] = None,
        audit_issues: Optional[list] = None,
    ) -> Optional[PendingAction]:
        """把某个待决操作推进到下一个状态。非法迁移返回 None（不抛异常）。"""
        item = self.get(pending_id)
        if item is None:
            return None
        allowed = _VALID_TRANSITIONS.get(item.status, set())
        if resolution not in allowed:
            return None
        item.status = resolution
        if audit_verdict is not None:
            item.audit_verdict = audit_verdict
        if audit_issues is not None:
            item.audit_issues = list(audit_issues)
        item.resolution_history.append(
            {"status": resolution, "step": step, "audit_verdict": audit_verdict}
        )
        return item

    def attach_result(self, pending_id: str, result: ToolResult, step: int = 0) -> Optional[PendingAction]:
        item = self.get(pending_id)
        if item is None:
            return None
        item.result = result
        self.resolve(pending_id, PENDING_EXECUTED, step=step)
        return self.resolve(pending_id, PENDING_AUDITED, step=step)

    # -- 读取 -------------------------------------------------------------

    def get(self, pending_id: str) -> Optional[PendingAction]:
        for item in self._items:
            if item.id == pending_id:
                return item
        return None

    def list(self) -> list:
        return list(self._items)

    def unresolved(self) -> list:
        return [i for i in self._items if i.status in (PENDING_PENDING, PENDING_REVISED, PENDING_APPROVED)]

    def by_level(self, level: str) -> list:
        return [i for i in self._items if i.decision.level == level]

    def intercepts(self) -> list:
        return self.by_level(RISK_INTERCEPT)

    def snapshot(self) -> dict:
        return {
            "turn": self.turn,
            "items": [i.to_dict() for i in self._items],
        }

    def __len__(self) -> int:
        return len(self._items)


def turn_label(turn: int) -> str:
    return f"t{turn}"
