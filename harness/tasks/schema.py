"""任务 schema 与校验（构建规范 §12.1/§12.5）。

`Task` 数据结构本体定义在 `core/types.py`（`core/` 与 `tasks/` 共用）；本模块负责
从 YAML 载入时的**严格校验**与错误聚合——任务集是实验的输入，静默接受坏任务
会污染全部指标。
"""

from __future__ import annotations

from typing import Optional

from core.types import Task, ValidationSpec

CATEGORIES = ("long_code", "long_text", "short_command", "mixed")
VALIDATION_KINDS = ("compile", "unit_test", "regex", "llm_judge", "manual")
DIFFICULTIES = ("easy", "medium", "hard")

# 每类任务的最低规模要求（§12.3）
MIN_ITEMS_PER_CATEGORY = 20


class TaskSchemaError(ValueError):
    pass


def task_from_dict(raw: dict, source: Optional[str] = None) -> Task:
    if not isinstance(raw, dict):
        raise TaskSchemaError(f"task entry must be a mapping, got {type(raw).__name__}")

    def require(field: str):
        if field not in raw or raw[field] in (None, ""):
            raise TaskSchemaError(f"{source or '<inline>'}: missing required field {field!r}")
        return raw[field]

    task_id = str(require("id"))
    category = str(require("category"))
    if category not in CATEGORIES:
        raise TaskSchemaError(f"{task_id}: unknown category {category!r}, expected one of {CATEGORIES}")
    prompt = str(require("prompt"))

    validation_raw = raw.get("validation") or {}
    if not isinstance(validation_raw, dict):
        raise TaskSchemaError(f"{task_id}: validation must be a mapping")
    kind = validation_raw.get("kind")
    if kind not in VALIDATION_KINDS:
        raise TaskSchemaError(f"{task_id}: validation.kind must be one of {VALIDATION_KINDS}, got {kind!r}")
    config = validation_raw.get("config") or {}
    if not isinstance(config, dict):
        raise TaskSchemaError(f"{task_id}: validation.config must be a mapping")

    timeout = raw.get("timeout", 300)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError) as exc:
        raise TaskSchemaError(f"{task_id}: timeout must be numeric") from exc
    if timeout <= 0:
        raise TaskSchemaError(f"{task_id}: timeout must be positive")

    for field in ("expected_error_prone_areas", "dangerous_operations"):
        if field in raw and not isinstance(raw[field], list):
            raise TaskSchemaError(f"{task_id}: {field} must be a list")

    difficulty = raw.get("baseline_difficulty", "medium")
    if difficulty not in DIFFICULTIES:
        raise TaskSchemaError(f"{task_id}: baseline_difficulty must be one of {DIFFICULTIES}")

    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise TaskSchemaError(f"{task_id}: metadata must be a mapping")

    return Task(
        id=task_id,
        category=category,
        prompt=prompt,
        validation=ValidationSpec(kind=kind, config=dict(config)),
        timeout=timeout,
        expected_output_schema=raw.get("expected_output_schema"),
        expected_error_prone_areas=list(raw.get("expected_error_prone_areas") or []),
        dangerous_operations=list(raw.get("dangerous_operations") or []),
        baseline_difficulty=difficulty,
        metadata=dict(metadata),
    )


def validate_tasks(tasks: list, strict: bool = True) -> list:
    """返回问题列表；`strict=True` 时直接抛 `TaskSchemaError`。"""
    problems: list = []
    seen: set = set()
    for task in tasks:
        if task.id in seen:
            problems.append(f"duplicate task id: {task.id}")
        seen.add(task.id)
        if not task.dangerous_operations and task.category == "short_command":
            problems.append(f"{task.id}: short_command task must declare dangerous_operations")
        if not task.expected_error_prone_areas:
            problems.append(f"{task.id}: expected_error_prone_areas is empty")
    if strict and problems:
        raise TaskSchemaError("; ".join(problems))
    return problems


def validate_suite(tasks: list, min_per_category: int = MIN_ITEMS_PER_CATEGORY, strict: bool = True) -> list:
    """校验整个任务集规模（§12.3）。"""
    problems = validate_tasks(tasks, strict=False)
    counts = {category: 0 for category in CATEGORIES}
    for task in tasks:
        counts[task.category] = counts.get(task.category, 0) + 1
    for category, count in counts.items():
        if count < min_per_category:
            problems.append(f"category {category} has {count} tasks, need >= {min_per_category}")
    if strict and problems:
        raise TaskSchemaError("; ".join(problems))
    return problems


def category_counts(tasks: list) -> dict:
    counts = {category: 0 for category in CATEGORIES}
    for task in tasks:
        counts[task.category] = counts.get(task.category, 0) + 1
    return counts
