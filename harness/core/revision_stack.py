"""RevisionStack：带射程锁的上下文编辑协议（构建规范 §6）。

铁律（§6.3 / 铁律 6 / 铁律 7）：
- 操作的是**字符串**，不是 token；
- `resolve_range` 只允许三级匹配：精确 → 去首尾空白 → 空白归一化；
  **禁止模糊匹配、禁止语义匹配**（v0.2 §2.4 的“语义线索做近似匹配”被 v1.0 否决，
  冲突以 v1.0 为准）。匹配失败即 `rejected`，写入审计，继续处理后续修订；
- 射程锁**硬编码在本层**，不靠提示词：历史消息、用户输入、系统消息在任何情况下
  都不可能被修改——`push` 只接受 `turn == self.turn` 且 `created_at >= turn_start_step`
  的修订，`finalize` 只在本回合的 `base_text` 上做区间替换，区间越界一律拒绝；
- 拒绝不抛异常，只写审计。
"""

from __future__ import annotations

import re
from typing import Optional

from core.types import (
    REV_APPLIED,
    REV_ERASE,
    REV_INSERT,
    REV_REJECTED,
    REV_REPLACE,
    REV_STAGED,
    FinalizeResult,
    Revision,
)


def resolve_range(text: str, target_text: str, created_at: int = 0) -> tuple:
    """（§6.3）返回 `(start, end)`；失败返回 `(-1, -1)`。

    `created_at` 参与签名是为了让排序语义显式化（应用顺序由 `finalize` 保证），
    本函数本身不做任何时间判断。
    """
    if not isinstance(target_text, str) or target_text == "":
        return (-1, -1)

    # 1. 精确匹配
    idx = text.find(target_text)
    if idx != -1:
        return (idx, idx + len(target_text))

    # 2. 忽略首尾空白
    stripped = target_text.strip()
    if stripped:
        idx = text.find(stripped)
        if idx != -1:
            return (idx, idx + len(stripped))

    # 3. 空白归一化（多个空白字符视为一个）
    words = target_text.split()
    if words:
        pattern = r"\s+".join(re.escape(w) for w in words)
        match = re.search(pattern, text)
        if match:
            return (match.start(), match.end())

    # 4. 失败
    return (-1, -1)


class RevisionStack:
    def __init__(self, turn: int = 0, base_text: str = ""):
        self.turn = turn
        self.base_text = base_text
        self.revisions: list = []
        self.turn_start_step = 0
        self.lock_violations: list = []
        self._counter = 0
        self._seen_ids: set = set()
        self._seen_objects: set = set()

    # -- 射程锁 -----------------------------------------------------------

    def start_turn(self, turn: int, base_text: str, start_step: int) -> None:
        self.turn = turn
        self.base_text = base_text
        self.turn_start_step = start_step
        self._counter = 0
        self._seen_ids = set()
        self._seen_objects = set()

    def _locked_texts(self) -> list:
        """射程锁保护的文本集合。

        实现说明（铁律 7）：射程锁不靠“文本清单比对”，而是靠**根本拿不到历史文本**——
        本栈只持有本回合 `base_text`，`_apply_one` 的所有区间替换都作用在这个字符串上，
        越界立即返回 None。历史消息与用户输入不在本栈可达范围内，因此不可能被修改。
        """
        return []

    # -- 写入 -------------------------------------------------------------

    def push(self, rev: Revision) -> bool:
        """入栈。校验射程锁与初始状态，违规**拒绝且不抛异常**。"""
        if rev.status != REV_STAGED:
            return self._reject_push(rev, f"initial status must be {REV_STAGED!r}, got {rev.status!r}")
        if rev.turn != self.turn:
            return self._reject_push(
                rev, f"range lock: revision turn {rev.turn} != stack turn {self.turn} (history is immutable)"
            )
        if rev.created_at < self.turn_start_step:
            return self._reject_push(
                rev,
                f"range lock: created_at {rev.created_at} < turn start step {self.turn_start_step}",
            )
        if rev.op not in (REV_INSERT, REV_ERASE, REV_REPLACE):
            return self._reject_push(rev, f"unknown op: {rev.op!r}")
        if not isinstance(rev.target_text, str) or rev.target_text == "":
            return self._reject_push(rev, "target_text must be a non-empty string")
        if rev.op in (REV_INSERT, REV_REPLACE) and rev.payload is None:
            return self._reject_push(rev, f"op {rev.op!r} requires payload")
        if rev.id in self._seen_ids or id(rev) in self._seen_objects:
            # 同一条修订被重复投递（provider 重发 / 重复消费 / 上层重复处理）→ 幂等拒绝。
            # 否则同一条修订会被重复应用，把正确内容改坏。
            return self._reject_push(rev, f"duplicate revision id: {rev.id!r}")
        # 定位预检：能在 base_text 或当前可应用中间态中找到（允许 finalize 时再失败）
        if resolve_range(self.base_text, rev.target_text, rev.created_at) == (-1, -1):
            preview = self.preview_text()
            if resolve_range(preview, rev.target_text, rev.created_at) == (-1, -1):
                self.lock_violations.append(
                    {"revision_id": rev.id, "reason": "target_text not found in current turn base_text"}
                )
        self._counter += 1
        if rev.id:
            self._seen_ids.add(rev.id)
        self._seen_objects.add(id(rev))
        # id 由栈分配并保证唯一：入参 id 只作为可追溯后缀（模型可能在同回合多次投递同名 id）
        rev.id = f"r{self.turn}_{self._counter}" + (f":{rev.id}" if rev.id else "")
        self.revisions.append(rev)
        return True

    def _reject_push(self, rev: Revision, reason: str) -> bool:
        rev.status = REV_REJECTED
        rev.rejected_reason = reason
        self.lock_violations.append({"revision_id": rev.id, "reason": reason})
        self.revisions.append(rev)
        return False

    def new_revision(
        self,
        turn: int,
        created_at: int,
        target_text: str,
        op: str,
        payload: str = "",
        reason: str = "",
    ) -> Revision:
        self._counter += 1
        return Revision(
            id=f"r{turn}_{self._counter}",
            turn=turn,
            created_at=created_at,
            target_text=target_text,
            op=op,
            payload=payload,
            reason=reason,
            status=REV_STAGED,
        )

    # -- 应用 -------------------------------------------------------------

    def staged(self) -> list:
        return [r for r in self.revisions if r.status == REV_STAGED]

    def preview_text(self) -> str:
        """在不改变修订状态的前提下，试算当前 staged 修订应用后的文本。"""
        text = self.base_text
        for rev in sorted(self.staged(), key=lambda r: (r.created_at, r.id)):
            result = _apply_one(text, rev)
            if result is not None:
                text = result
        return text

    def finalize(self) -> FinalizeResult:
        """（§6.2/§6.4）按 `created_at` 升序应用所有 staged 修订。

        - 应用第 N 条时基于已应用前 N-1 条的结果重新定位；
        - 定位失败 → `rejected`，继续处理后续；
        - 不允许回滚已应用的修订。
        """
        text = self.base_text
        applied: list = []
        rejected: list = []

        ordered = sorted(self.staged(), key=lambda r: (r.created_at, r.id))
        for rev in ordered:
            new_text = _apply_one(text, rev)
            if new_text is None:
                rev.status = REV_REJECTED
                if not rev.rejected_reason:
                    start, _ = resolve_range(text, rev.target_text, rev.created_at)
                    rev.rejected_reason = (
                        "exact/whitespace-normalized match failed; fuzzy matching is forbidden by spec §6.3"
                        if start == -1
                        else "resolved range out of bounds"
                    )
                rev.resolved_range = None
                rejected.append(rev)
                continue
            rev.status = REV_APPLIED
            applied.append(rev)
            text = new_text

        return FinalizeResult(
            finalized_text=text,
            applied=applied,
            rejected=rejected,
            cache_break_point=_first_difference(self.base_text, text),
        )

    # -- 审计 -------------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "turn": self.turn,
            "turn_start_step": self.turn_start_step,
            "base_text": self.base_text,
            "base_text_len": len(self.base_text),
            "revisions": [r.to_dict() for r in self.revisions],
            "lock_violations": list(self.lock_violations),
        }

    @property
    def base_text_len(self) -> int:
        return len(self.base_text)


def _apply_one(text: str, rev: Revision) -> Optional[str]:
    """把一条修订应用到 `text`；失败返回 None。越界一律拒绝（射程锁）。"""
    start, end = resolve_range(text, rev.target_text, rev.created_at)
    if start == -1:
        return None
    if start < 0 or end > len(text) or end < start:
        return None
    rev.resolved_range = (start, end)
    if rev.op == REV_REPLACE:
        return text[:start] + (rev.payload or "") + text[end:]
    if rev.op == REV_ERASE:
        return text[:start] + text[end:]
    if rev.op == REV_INSERT:
        # target_text 只做定位，不删除；payload 追加在定位到的片段之后。
        return text[:end] + (rev.payload or "") + text[end:]
    return None


def _first_difference(a: str, b: str) -> int:
    """`cache_break_point`：两者最早不同的字符位置；完全相同取 max(len)。"""
    limit = min(len(a), len(b))
    for i in range(limit):
        if a[i] != b[i]:
            return i
    if len(a) == len(b):
        return len(a)
    return limit
