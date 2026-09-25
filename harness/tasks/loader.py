"""任务加载器（构建规范 §12.4）。

`configs/tasks/*.yaml` 每个文件是一个任务列表；`load_tasks` 负责按类别过滤、
按文件顺序稳定排序（保证 runner 的可复现性），并做 schema 校验。
"""

from __future__ import annotations

import os
from typing import Iterable, Optional

import yaml

from core.types import Task
from tasks.schema import (
    CATEGORIES,
    TaskSchemaError,
    category_counts,
    task_from_dict,
    validate_suite,
    validate_tasks,
)

DEFAULT_TASKS_DIR = os.path.join("configs", "tasks")


def tasks_dir(base_dir: Optional[str] = None) -> str:
    return base_dir or DEFAULT_TASKS_DIR


def load_tasks_file(path: str) -> list:
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = raw.get("tasks", [])
    if not isinstance(raw, list):
        raise TaskSchemaError(f"{path}: top level must be a list of tasks")
    tasks = []
    for entry in raw:
        task = task_from_dict(entry, source=path)
        task.metadata.setdefault("source_file", os.path.basename(path))
        tasks.append(task)
    return tasks


def load_tasks(
    base_dir: Optional[str] = None,
    categories: Optional[Iterable[str]] = None,
    task_ids: Optional[Iterable[str]] = None,
    strict: bool = True,
) -> list:
    """载入任务集。

    - `categories`：只保留这些类别；
    - `task_ids`：只保留这些 id（顺序按 id 列表给定顺序）；
    - 返回顺序固定（先按文件名，再按文件内顺序），保证可复现。
    """
    directory = tasks_dir(base_dir)
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"tasks directory not found: {directory}")
    files = sorted(f for f in os.listdir(directory) if f.endswith((".yaml", ".yml")))
    tasks: list = []
    for name in files:
        tasks.extend(load_tasks_file(os.path.join(directory, name)))

    problems = validate_tasks(tasks, strict=False)
    if strict and problems:
        raise TaskSchemaError("; ".join(problems))

    if categories is not None:
        wanted = {c for c in categories}
        unknown = wanted - set(CATEGORIES)
        if unknown:
            raise TaskSchemaError(f"unknown categories: {sorted(unknown)}")
        tasks = [t for t in tasks if t.category in wanted]

    if task_ids is not None:
        index = {t.id: t for t in tasks}
        selected = []
        for task_id in task_ids:
            if task_id not in index:
                raise TaskSchemaError(f"unknown task id: {task_id}")
            selected.append(index[task_id])
        tasks = selected

    if strict:
        problems = validate_suite(tasks, strict=False) if task_ids is None else validate_tasks(tasks, strict=False)
        if problems:
            raise TaskSchemaError("; ".join(problems))
    return tasks


def select_tasks(tasks: list, task_ids: Optional[Iterable[str]] = None) -> list:
    if not task_ids:
        return list(tasks)
    index = {t.id: t for t in tasks}
    missing = [tid for tid in task_ids if tid not in index]
    if missing:
        raise TaskSchemaError(f"task ids not present in task set: {missing}")
    return [index[tid] for tid in task_ids]


def stratified_limit(tasks: list, limit: Optional[int]) -> list:
    """按类别轮转取样，保证 `limit` 在各任务类别之间均衡分配。

    直接切片会让 `limit < 总任务数` 时只取到第一个文件（例如全部 long_code），
    使分层分析与“每类都要有样本”的验收失效。这里按类别轮转取前 `limit` 个，
    顺序仍然完全确定（类别内保持文件顺序）。
    """
    if not limit or limit >= len(tasks):
        return list(tasks)
    buckets: dict = {}
    for task in tasks:
        buckets.setdefault(task.category, []).append(task)
    order = list(CATEGORIES)
    for category in buckets:
        if category not in order:
            order.append(category)
    picked: list = []
    index = 0
    while len(picked) < limit:
        advanced = False
        for category in order:
            bucket = buckets.get(category) or []
            if index < len(bucket):
                picked.append(bucket[index])
                advanced = True
                if len(picked) >= limit:
                    break
        if not advanced:
            break
        index += 1
    return picked


__all__ = [
    "CATEGORIES",
    "category_counts",
    "load_tasks",
    "load_tasks_file",
    "select_tasks",
    "stratified_limit",
    "task_from_dict",
    "tasks_dir",
    "validate_suite",
    "Task",
]
