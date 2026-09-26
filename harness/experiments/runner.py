"""实验运行器（构建规范 §11）。

核心要求：**一个 harness 跑完全部实验组，通过配置切换，不修改任何代码**。

- `ExperimentConfig` 从 `configs/base.yaml` + `configs/groups/<X>.yaml` 合并得到；
- H 组自动展开为 H-none / H-weak / H-strong 三个子组，共享同一任务集与采样参数；
- 唯一变量：`prompt_condition` + `enabled_mechanisms`（铁律 8）；
- 失败 run 不静默丢弃：记录 `error` 事件、写入 `metrics.jsonl`、计入报告；
- 确定性：固定任务顺序、固定 runs_per_task、固定采样参数、失败不补跑。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

import yaml

from core.audit_logger import AuditLogger
from core.executor import DryRunExecutor
from core.payload_builder import SYSTEM_PROMPT_BASE
from core.types import (
    EnabledMechanisms,
    ExperimentConfig,
    PROMPT_NONE,
    PROMPT_STRONG,
    PROMPT_WEAK,
    SamplingConfig,
    to_jsonable,
)
from experiments.harness import DeterministicClock, MechanismHarness
from experiments.metrics import run_metrics_from_result
from tasks.loader import load_tasks, select_tasks, stratified_limit

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIGS_DIR = os.path.join(HARNESS_DIR, "configs")
DEFAULT_RUNS_DIR = os.path.join(HARNESS_DIR, "runs")

GROUP_ORDER = ["A", "B", "C", "D", "E", "F", "G", "H"]

MECHANISM_KEYS = ("risk_router", "tail_audit", "external_auditor", "revision_stack")


class ConfigError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------


def _read_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def load_base_config(configs_dir: Optional[str] = None) -> dict:
    directory = configs_dir or CONFIGS_DIR
    path = os.path.join(directory, "base.yaml")
    if not os.path.isfile(path):
        raise ConfigError(f"base config not found: {path}")
    return _read_yaml(path)


def _group_file(group: str, configs_dir: str) -> str:
    matches = [
        name
        for name in sorted(os.listdir(os.path.join(configs_dir, "groups")))
        if name.startswith(f"{group}_") and name.endswith((".yaml", ".yml"))
    ]
    if not matches:
        raise ConfigError(f"no config found for group {group!r} in {configs_dir}/groups")
    return os.path.join(configs_dir, "groups", matches[0])


def _merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key == "enabled_mechanisms" and isinstance(value, dict):
            merged = dict(result.get("enabled_mechanisms") or {})
            merged.update(value)
            result[key] = merged
        elif key == "sampling" and isinstance(value, dict):
            merged = dict(result.get("sampling") or {})
            merged.update(value)
            result[key] = merged
        else:
            result[key] = copy.deepcopy(value)
    return result


def apply_connection_overrides(
    configs: list,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> list:
    """把启动器/CLI 传入的连接参数打到每组配置上（§11.5 固定模型版本）。

    - `api_key` 只挂在内存里的 config 上，随后由 provider 客户端读取；
      **绝不写入配置文件、日志或审计**（审计只记 model / base_url / request_id）。
    - `model` 覆盖 `sampling.model`：真实 API 必须写死具体版本，禁止滚动别名。
    """
    for config in configs:
        if provider:
            config.provider = provider  # type: ignore[attr-defined]
            config.sampling.provider = provider
        if base_url:
            config.base_url = base_url  # type: ignore[attr-defined]
        if api_key:
            config.api_key = api_key  # type: ignore[attr-defined]
        if model:
            config.sampling.model = model
    return configs


def build_group_configs(
    group: str,
    configs_dir: Optional[str] = None,
    task_limit: Optional[int] = None,
    runs_override: Optional[int] = None,
    tasks_dir: Optional[str] = None,
) -> list:
    """返回该组的 `ExperimentConfig` 列表（H 组返回 3 个子组）。

    `tasks_dir` 可显式指定任务集目录：打包成单文件 exe 时配置文件被解包到临时目录，
    必须由调用方注入正确路径，不能依赖相对路径推断。
    """
    directory = configs_dir or CONFIGS_DIR
    base = load_base_config(directory)
    path = _group_file(group, directory)
    raw = _merge(base, _read_yaml(path))

    task_cfg = raw.get("tasks", {})
    if isinstance(task_cfg, list):
        task_ids = list(task_cfg)
        categories = None
    else:
        task_ids = list(task_cfg.get("include") or [])
        categories = task_cfg.get("categories")
    limit = task_limit if task_limit is not None else task_cfg.get("limit")
    runs_per_task = runs_override if runs_override is not None else raw.get("runs_per_task", 1)

    all_tasks = load_tasks(
        base_dir=tasks_dir or os.path.join(directory, "tasks"),
        categories=categories,
        strict=False,
    )
    if task_ids:
        all_tasks = select_tasks(all_tasks, task_ids)
    if limit:
        all_tasks = stratified_limit(all_tasks, int(limit))
    if not all_tasks:
        raise ConfigError(f"group {group}: empty task selection")

    sampling_raw = raw.get("sampling") or {}
    timeout = float(raw.get("timeout", 300.0))
    mechanics = raw.get("enabled_mechanisms") or {}
    enabled = EnabledMechanisms(
        risk_router=bool(mechanics.get("risk_router", False)),
        tail_audit=bool(mechanics.get("tail_audit", False)),
        external_auditor=bool(mechanics.get("external_auditor", False)),
        revision_stack=bool(mechanics.get("revision_stack", False)),
    )

    configs: list = []
    if group == "H":
        matrix = raw.get("prompt_matrix") or [PROMPT_NONE, PROMPT_WEAK, PROMPT_STRONG]
        for condition in matrix:
            configs.append(
                _make_config(
                    group="H",
                    sub_group=f"H-{condition}",
                    prompt_condition=condition,
                    enabled=enabled,
                    tasks=all_tasks,
                    runs_per_task=runs_per_task,
                    sampling_raw=sampling_raw,
                    timeout=timeout,
                    raw=raw,
                    source_path=path,
                )
            )
    else:
        configs.append(
            _make_config(
                group=group,
                sub_group=None,
                prompt_condition=raw.get("prompt_condition", PROMPT_NONE),
                enabled=enabled,
                tasks=all_tasks,
                runs_per_task=runs_per_task,
                sampling_raw=sampling_raw,
                timeout=timeout,
                raw=raw,
                source_path=path,
            )
        )
    return configs


def _make_config(
    group: str,
    sub_group: Optional[str],
    prompt_condition: str,
    enabled: EnabledMechanisms,
    tasks: list,
    runs_per_task: int,
    sampling_raw: dict,
    timeout: float,
    raw: dict,
    source_path: str,
) -> ExperimentConfig:
    sampling = SamplingConfig(
        model=sampling_raw.get("model", "fake-model-v1"),
        temperature=float(sampling_raw.get("temperature", 0.0)),
        top_p=float(sampling_raw.get("top_p", 1.0)),
        max_tokens=int(sampling_raw.get("max_tokens", 4096)),
        seed=sampling_raw.get("seed", 0),
        provider=sampling_raw.get("provider", raw.get("provider", "fake")),
    )
    config = ExperimentConfig(
        group=group,
        sub_group=sub_group,
        prompt_condition=prompt_condition,
        enabled_mechanisms=enabled,
        tasks=[task.id for task in tasks],
        runs_per_task=int(runs_per_task),
        sampling=sampling,
        timeout=timeout,
        source_path=source_path,
    )
    # 运行期附加字段（不属于 ExperimentConfig 契约，但需要随运行走）
    config.task_objects = tasks  # type: ignore[attr-defined]
    config.provider = raw.get("provider", sampling.provider)  # type: ignore[attr-defined]
    config.tail_audit_timeout = float(raw.get("tail_audit_timeout", 30.0))  # type: ignore[attr-defined]
    config.auditor_timeout = float(raw.get("auditor_timeout", 20.0))  # type: ignore[attr-defined]
    config.auditor_temperature = float(raw.get("auditor_temperature", 0.7))  # type: ignore[attr-defined]
    config.system_prompt_base = raw.get("system_prompt_base", SYSTEM_PROMPT_BASE)  # type: ignore[attr-defined]
    config.executor_dry_run = bool(raw.get("executor_dry_run", True))  # type: ignore[attr-defined]
    config.fake_tail_audit_fixes = bool(raw.get("fake_tail_audit_fixes", True))  # type: ignore[attr-defined]
    config.fake_external_dangerous_tool = bool(raw.get("fake_external_dangerous_tool", True))  # type: ignore[attr-defined]
    config.fake_tail_audit_verdict = raw.get("fake_tail_audit_verdict", "approve")  # type: ignore[attr-defined]
    config.fake_audit_raw_override = raw.get("fake_audit_raw_override")  # type: ignore[attr-defined]
    # 真实 provider 的连接参数：由调用方（CLI / GUI 启动器）注入，绝不落盘
    config.api_key = None  # type: ignore[attr-defined]
    config.base_url = None  # type: ignore[attr-defined]
    config.max_retries = int(raw.get("max_retries", 3))  # type: ignore[attr-defined]
    return config

# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------


@dataclass
class RunSummary:
    run_id: str
    group: str
    sub_group: Optional[str]
    task_id: str
    category: str
    metrics: dict = field(default_factory=dict)
    error: Optional[str] = None


_CLIENT_CACHE: dict = {}


def build_offline_client(config: ExperimentConfig, role: str = "main"):
    """按配置构造客户端。

    - `provider=fake`：离线确定性客户端。按 `(role, provider, 是否模拟生成期主动登记修订)`
      缓存实例 —— 主模型与审核 Agent 必须是**不同实例**，H 三个子组也不能共用同一实例
      （否则 `set_task` 会互相污染）。
    - 真实 provider：按 §11.6 包一层重试（最多 3 次、指数退避），并注入 API 地址与密钥。
      密钥来源优先级：CLI 显式传入 > 环境变量。**不缓存**（每次都新建）。
    """
    provider = getattr(config, "provider", "fake")
    if provider == "fake":
        from llm.fake_client import FakeClient

        # 提示词条件 → 生成期是否主动登记修订（离线模型对提示词强度的响应，
        # 用于 H 组的能力边界矩阵与 G 组的“假设后训练”增量）
        spontaneous = config.prompt_condition != PROMPT_NONE
        key = (role, provider, spontaneous)
        if key not in _CLIENT_CACHE:
            _CLIENT_CACHE[key] = FakeClient(
                tail_audit_fixes=getattr(config, "fake_tail_audit_fixes", True),
                external_dangerous_tool=getattr(config, "fake_external_dangerous_tool", True),
                tail_audit_verdict=getattr(config, "fake_tail_audit_verdict", "approve"),
                audit_raw_override=getattr(config, "fake_audit_raw_override", None),
                spontaneous_revision=spontaneous,
            )
        return _CLIENT_CACHE[key]

    from llm.base_client import RetryingClient

    if provider == "openai":
        from llm.openai_client import OpenAIClient

        client = OpenAIClient(
            api_key=_resolve_api_key(config, "OPENAI_API_KEY"),
            model=config.sampling.model,
            base_url=getattr(config, "base_url", None) or os.environ.get(
                "OPENAI_BASE_URL", "https://api.openai.com/v1"
            ),
        )
        return RetryingClient(client, max_retries=int(getattr(config, "max_retries", 3)))
    if provider == "anthropic":
        from llm.anthropic_client import AnthropicClient

        client = AnthropicClient(
            api_key=_resolve_api_key(config, "ANTHROPIC_API_KEY"),
            model=config.sampling.model,
            base_url=getattr(config, "base_url", None) or os.environ.get(
                "ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"
            ),
        )
        return RetryingClient(client, max_retries=int(getattr(config, "max_retries", 3)))
    raise ConfigError(f"unknown provider: {provider!r}")


def _resolve_api_key(config: ExperimentConfig, env_name: str) -> str:
    key = getattr(config, "api_key", None) or os.environ.get(env_name, "")
    if not key:
        raise ConfigError(
            f"provider {getattr(config, 'provider', '?')!r} 需要 API key："
            f"用 --api-key 传入或设置环境变量 {env_name}（绝不会写进配置或日志）"
        )
    return key


def run_single(config: ExperimentConfig, task, run_index: int, out_dir: str, executor=None) -> RunSummary:
    group_label = config.sub_group or config.group
    run_id = f"{group_label}-{task.id}-r{run_index}"
    client = build_offline_client(config, role="main")
    if hasattr(client, "set_task"):
        client.set_task(task)
    auditor_client = None
    if config.enabled_mechanisms.external_auditor:
        # 审核 Agent 必须与主模型是不同实例（§8.5：不同 prompt / 不同 temperature）
        auditor_client = build_offline_client(config, role="auditor")
        if hasattr(auditor_client, "set_task"):
            auditor_client.set_task(task)

    audit_path = os.path.join(out_dir, "audit", f"{run_id}.jsonl")
    logger = AuditLogger(path=audit_path, run_id=run_id, group=group_label, task_id=task.id)
    # 离线 provider 使用固定步长时钟，保证指标（含 latency_ms）可复现（§14.4）
    clock = DeterministicClock() if getattr(config, "provider", "fake") == "fake" else None
    harness = MechanismHarness(
        config=config,
        task=task,
        client=client,
        auditor_client=auditor_client,
        executor=executor if executor is not None else DryRunExecutor(),
        audit_logger=logger,
        system_prompt_base=getattr(config, "system_prompt_base", SYSTEM_PROMPT_BASE),
        run_id=run_id,
        clock=clock,
    )
    result = harness.run()
    logger.close()

    metrics = run_metrics_from_result(result)
    metrics["audit_log_bytes"] = os.path.getsize(audit_path) if os.path.isfile(audit_path) else 0
    payload = {
        "run_id": run_id,
        "group": group_label,
        "task_id": task.id,
        "category": task.category,
        "metrics": metrics,
        "error": result.error,
    }
    os.makedirs(os.path.join(out_dir, "runs"), exist_ok=True)
    with open(os.path.join(out_dir, "runs", f"{run_id}.json"), "w", encoding="utf-8") as handle:
        json.dump(to_jsonable(payload), handle, ensure_ascii=False, indent=2, sort_keys=True)

    return RunSummary(
        run_id=run_id,
        group=group_label,
        sub_group=config.sub_group,
        task_id=task.id,
        category=task.category,
        metrics=metrics,
        error=result.error,
    )


def run_experiment(
    group: str,
    out_dir: Optional[str] = None,
    configs_dir: Optional[str] = None,
    task_limit: Optional[int] = None,
    runs_override: Optional[int] = None,
    task_ids: Optional[list] = None,
    verbose: bool = True,
    metrics_mode: str = "group",
    tasks_dir: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """跑一个（或多个子）组的全部任务，返回汇总字典。

    `metrics_mode`：
    - `"group"`（默认）：把本次组的结果写到 `runs/metrics/<group>.jsonl`，
      不覆盖 `runs/metrics.jsonl` —— 逐组运行时可累积，跑完全部组后再汇总；
    - `"overwrite"`：直接覆盖 `runs/metrics.jsonl`；
    - `"append"`：追加到 `runs/metrics.jsonl`。
    """
    out_dir = out_dir or DEFAULT_RUNS_DIR
    os.makedirs(os.path.join(out_dir, "audit"), exist_ok=True)
    configs = build_group_configs(
        group,
        configs_dir=configs_dir,
        task_limit=task_limit,
        runs_override=runs_override,
        tasks_dir=tasks_dir,
    )
    apply_connection_overrides(
        configs, provider=provider, base_url=base_url, api_key=api_key, model=model
    )
    summary = {
        "out_dir": out_dir,
        "groups": [],
        "metrics": [],
        "failures": 0,
    }
    label_seen = []
    for config in configs:
        group_label = config.sub_group or config.group
        label_seen.append(group_label)
        tasks = select_tasks(getattr(config, "task_objects"), task_ids) if task_ids else getattr(config, "task_objects")
        entries: list = []
        for task in tasks:
            for run_index in range(config.runs_per_task):
                entry = run_single(config, task, run_index, out_dir)
                entries.append(entry)
                if entry.error:
                    summary["failures"] += 1
                if verbose:
                    metrics = entry.metrics
                    print(
                        f"[{group_label}] {task.id} ({task.category}) "
                        f"success={metrics['task_success']} syntax_err={metrics['syntax_error']} "
                        f"intercepts={metrics['intercepts']} calls={metrics['llm_calls']}"
                        + (f" error={entry.error}" if entry.error else "")
                    )
        summary["groups"].append(
            {
                "group": group_label,
                "prompt_condition": config.prompt_condition,
                "enabled_mechanisms": config.enabled_mechanisms.to_dict(),
                "runs": len(entries),
                "failures": sum(1 for e in entries if e.error),
            }
        )
        summary["metrics"].extend(
            [
                {
                    "run_id": e.run_id,
                    "group": group_label,
                    "task_id": e.task_id,
                    "category": e.category,
                    **e.metrics,
                }
                for e in entries
            ]
        )

    metrics_path = os.path.join(out_dir, "metrics.jsonl")
    if metrics_mode == "group":
        # 文件名按组参数（不是子组标签）命名：H 组的三个子组写同一份文件，
        # 这样重复执行同一组只会覆盖自己那份，collect_group_metrics 不会重复累计。
        write_metrics(os.path.join(out_dir, "metrics", f"{group}.jsonl"), summary["metrics"])
    else:
        write_metrics(metrics_path, summary["metrics"], append=metrics_mode == "append")
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as handle:
        json.dump(to_jsonable({k: v for k, v in summary.items() if k != "metrics"}), handle,
                  ensure_ascii=False, indent=2, sort_keys=True)
    return summary


def collect_group_metrics(out_dir: str) -> list:
    """把所有组的 `metrics/<group>.jsonl` 合并成一份有序的完整指标列表。"""
    directory = os.path.join(out_dir, "metrics")
    rows: list = []
    if not os.path.isdir(directory):
        return rows
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(directory, name), "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def write_metrics(path: str, rows: list, append: bool = False) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    mode = "a" if append else "w"
    with open(path, mode, encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(to_jsonable(row), ensure_ascii=False, sort_keys=True) + "\n")


def run_all(
    out_dir: Optional[str] = None,
    configs_dir: Optional[str] = None,
    task_limit: Optional[int] = None,
    runs_override: Optional[int] = None,
    groups: Optional[list] = None,
    verbose: bool = True,
    tasks_dir: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    groups = groups or GROUP_ORDER
    out_dir = out_dir or DEFAULT_RUNS_DIR
    combined = {"groups": [], "metrics": [], "failures": 0, "out_dir": out_dir}
    for group in groups:
        summary = run_experiment(
            group,
            out_dir=out_dir,
            configs_dir=configs_dir,
            task_limit=task_limit,
            runs_override=runs_override,
            verbose=verbose,
            metrics_mode="group",
            tasks_dir=tasks_dir,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
        )
        combined["groups"].extend(summary["groups"])
        combined["failures"] += summary["failures"]
    rows = collect_group_metrics(out_dir)
    combined["metrics"] = rows
    write_metrics(os.path.join(out_dir, "metrics.jsonl"), rows)
    return combined


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="后悔承诺门 harness 实验运行器")
    parser.add_argument("--group", default="all", help="A–H 之一，或 all（默认 all）")
    parser.add_argument("--out", default=DEFAULT_RUNS_DIR, help="输出目录（默认 harness/runs）")
    parser.add_argument("--configs", default=CONFIGS_DIR, help="配置目录")
    parser.add_argument("--limit", type=int, default=None, help="每组任务上限（覆盖配置）")
    parser.add_argument("--runs", type=int, default=None, help="每任务重复次数（覆盖配置）")
    parser.add_argument("--quiet", action="store_true", help="不打印每 run 进度")
    args = parser.parse_args(argv)

    if args.group.lower() == "all":
        groups = GROUP_ORDER
    else:
        groups = [g.strip().upper() for g in args.group.split(",") if g.strip()]
        unknown = [g for g in groups if g not in GROUP_ORDER]
        if unknown:
            parser.error(f"unknown group(s): {unknown}")

    summary = run_all(
        out_dir=args.out,
        configs_dir=args.configs,
        task_limit=args.limit,
        runs_override=args.runs,
        groups=groups,
        verbose=not args.quiet,
    )
    print(
        f"\n完成：{len(summary['metrics'])} runs，失败 {summary['failures']}，"
        f"输出目录 {os.path.abspath(args.out)}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
