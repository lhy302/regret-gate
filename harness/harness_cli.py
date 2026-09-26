"""统一入口（CLI + GUI 启动器）。

打包成 exe 后支持两种用法：

```
regret-gate-harness.exe                 # 不带参数 → 打开图形启动器（填 API 地址/密钥/模型）
regret-gate-harness.exe selftest        # 自检：不联网，验证打包完整与链路可用
regret-gate-harness.exe run --provider fake --limit 4
regret-gate-harness.exe run --provider openai --base-url ... --model gpt-4o-2024-08-06 --group A,F
regret-gate-harness.exe report --out output
regret-gate-harness.exe init --out workdir
```

安全约定：
- `--api-key` 也可以用环境变量 `REGRET_GATE_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`；
  启动器（GUI）只通过环境变量把密钥传给子进程，不进命令行；
- 密钥**绝不写进配置文件、审计日志或报告**，审计里只记 `model` / `base_url` / `request_id`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

if __package__ in (None, ""):  # 直接以脚本方式运行（源码模式）
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _ensure_streams() -> None:
    """windowed exe 没有控制台：stdout/stderr 可能是 None 或已关闭，统一兜住。

    否则任何一次 print 都会抛异常，而这种情况在 GUI 启动的子进程里最难排查。
    同时把输出**落盘**到 `<可写根>/output/console.log` —— 打包后没有控制台，
    没有这行日志就只能靠猜。

    必须在 `experiments.paths` 导入之后调用（见文件末尾的 `_ensure_streams()`）。
    """
    log_path = os.path.join(paths.default_out_dir(), "console.log")
    devnull = None
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or getattr(stream, "closed", False):
            if devnull is None:
                try:
                    os.makedirs(os.path.dirname(log_path), exist_ok=True)
                    devnull = open(os.devnull, "w", encoding="utf-8")
                except OSError:
                    return
            setattr(sys, name, devnull)
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        handle = open(log_path, "a", encoding="utf-8", newline="\n")
        handle.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                     f"argv={sys.argv[1:]} frozen={paths.is_frozen()} ===\n")
        handle.flush()
        sys.stdout = _Tee(sys.stdout, handle)
        sys.stderr = _Tee(sys.stderr, handle)
    except OSError:
        pass


class _Tee:
    """把写入同时送到原流与日志文件；任一失败都不影响程序继续跑。"""

    def __init__(self, primary, secondary):
        self._primary = primary
        self._secondary = secondary

    def write(self, data):
        written = 0
        for stream in (self._primary, self._secondary):
            try:
                written = stream.write(data)
            except (OSError, ValueError):
                continue
        try:
            self._secondary.flush()
        except (OSError, ValueError):
            pass
        return written

    def flush(self):
        for stream in (self._primary, self._secondary):
            try:
                stream.flush()
            except (OSError, ValueError):
                continue

    def isatty(self) -> bool:
        return False

    def fileno(self):
        raise OSError("tee stream has no fileno")

from core.audit_logger import NullAuditLogger  # noqa: E402
from core.payload_builder import SYSTEM_PROMPT_BASE  # noqa: E402
from experiments import paths  # noqa: E402
from experiments.analysis import load_metrics  # noqa: E402
from experiments.harness import DeterministicClock, MechanismHarness  # noqa: E402
from experiments.metrics import run_metrics_from_result  # noqa: E402
from experiments.report import write_report  # noqa: E402
from experiments.runner import GROUP_ORDER, build_group_configs, run_all  # noqa: E402
from tasks.loader import load_tasks  # noqa: E402

# 必须在 import 之后：_ensure_streams 依赖 experiments.paths
_ensure_streams()

APP_NAME = "后悔承诺门 · Harness"
VERSION = "1.0.0"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2


# ---------------------------------------------------------------------------
# 公共
# ---------------------------------------------------------------------------


def resolve_api_key(args) -> str:
    for name in (
        getattr(args, "api_key", None),
        os.environ.get("REGRET_GATE_API_KEY"),
        os.environ.get("OPENAI_API_KEY") if args.provider == "openai" else None,
        os.environ.get("ANTHROPIC_API_KEY") if args.provider == "anthropic" else None,
    ):
        if name:
            return str(name)
    return ""


def resolve_base_url(args) -> str:
    if getattr(args, "base_url", None):
        return args.base_url
    env_name = "ANTHROPIC_BASE_URL" if args.provider == "anthropic" else "OPENAI_BASE_URL"
    return os.environ.get("REGRET_GATE_BASE_URL") or os.environ.get(env_name, "")


def parse_groups(raw: str) -> list:
    if not raw or raw.lower() == "all":
        return list(GROUP_ORDER)
    groups = [g.strip().upper() for g in raw.split(",") if g.strip()]
    unknown = [g for g in groups if g not in GROUP_ORDER]
    if unknown:
        raise SystemExit(f"未知实验组：{unknown}（可选 {GROUP_ORDER} 或 all）")
    return groups


# ---------------------------------------------------------------------------
# selftest：不联网，验证打包完整与核心链路
# ---------------------------------------------------------------------------


def cmd_selftest(args) -> int:
    report: dict = {"ok": True, "checks": []}

    def check(name: str, fn) -> None:
        try:
            detail = fn()
            report["checks"].append({"name": name, "ok": True, "detail": detail})
        except Exception as exc:  # noqa: BLE001
            report["ok"] = False
            report["checks"].append({"name": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}"})

    def check_paths():
        info = paths.describe_paths()
        if not os.path.isdir(info["configs_dir"]):
            raise FileNotFoundError(f"configs 目录缺失：{info['configs_dir']}")
        return info

    def check_tasks():
        tasks = load_tasks(base_dir=paths.bundled_tasks_dir())
        if len(tasks) != 80:
            raise AssertionError(f"任务集应为 80 个，实际 {len(tasks)}")
        return f"{len(tasks)} tasks"

    def check_configs():
        counts = {}
        for group in GROUP_ORDER:
            configs = build_group_configs(
                group, configs_dir=paths.bundled_configs_dir(), task_limit=4,
                tasks_dir=paths.bundled_tasks_dir(),
            )
            counts[group] = len(configs)
        return counts

    def check_pipeline():
        """跑一个离线任务，验证 拦截→自审→finalize→审核 全链路在打包环境可用。"""
        configs = build_group_configs(
            "F", configs_dir=paths.bundled_configs_dir(), task_limit=4,
            tasks_dir=paths.bundled_tasks_dir(),
        )
        config = configs[0]
        task = next(t for t in config.task_objects if t.category == "short_command")
        from llm.fake_client import FakeClient

        client = FakeClient()
        client.set_task(task)
        auditor = FakeClient()
        auditor.set_task(task)
        harness = MechanismHarness(
            config=config, task=task, client=client, auditor_client=auditor,
            audit_logger=NullAuditLogger(run_id="selftest", group="F", task_id=task.id),
            system_prompt_base=SYSTEM_PROMPT_BASE, run_id="selftest",
            clock=DeterministicClock(),
        )
        result = harness.run()
        metrics = run_metrics_from_result(result)
        if metrics["intercepts"] < 1:
            raise AssertionError("高危命令未被 RiskRouter 拦截（机制未生效）")
        if metrics["llm_calls"] > 10:
            raise AssertionError(f"单 run API 调用超预算：{metrics['llm_calls']}")
        return {
            "task": task.id,
            "intercepts": metrics["intercepts"],
            "security_incident": metrics["security_incident"],
            "llm_calls": metrics["llm_calls"],
        }

    def check_report():
        rows = [
            {
                "run_id": "selftest", "group": "F", "task_id": "sc_001", "category": "short_command",
                "syntax_error": False, "logic_error": False, "task_success": True,
                "security_incident": False, "false_intercept": False, "input_tokens": 10,
                "output_tokens": 10, "latency_ms": 1, "tail_audit_modified": True,
                "external_audit_effective": False, "revision_hit_rate": 1.0,
                "cache_break_ratio": 0.5, "intercepts": 1, "intercept_rules": [],
                "llm_calls": 2, "audit_log_bytes": 100, "error": None,
            }
        ]
        text = write_report(rows, os.path.join(args.work or paths.default_out_dir(), "selftest_report.md"),
                            meta={"provider": "fake", "source": "selftest"})
        if "假设 H1" not in text:
            raise AssertionError("报告模板不完整")
        return "report template ok"

    check("资源与目录", check_paths)
    check("任务集加载（80）", check_tasks)
    check("A–H 配置加载", check_configs)
    check("核心链路（离线单任务）", check_pipeline)
    check("报告模板", check_report)

    report["app_root"] = paths.app_root()
    report["frozen"] = paths.is_frozen()
    report["executable"] = sys.executable
    report["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(text)
    print("SELFTEST_OK" if report["ok"] else "SELFTEST_FAILED")

    # 打包成 windowed exe 后没有控制台，必须把结论落盘才能被验证
    try:
        evidence = os.path.join(args.work or paths.default_out_dir(), "selftest.json")
        os.makedirs(os.path.dirname(os.path.abspath(evidence)), exist_ok=True)
        with open(evidence, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")
        print(f"自检结论已写入：{os.path.abspath(evidence)}")
    except OSError as exc:
        print(f"[警告] 无法写入自检结论：{exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK if report["ok"] else EXIT_ERROR


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def cmd_run(args) -> int:
    groups = parse_groups(args.group)
    provider = args.provider.lower()
    model = args.model or ("fake-model-v1" if provider == "fake" else "")
    if provider == "fake" and args.model:
        model = args.model
    if provider != "fake" and not model:
        print("错误：真实 provider 必须显式指定 --model（写死具体版本，禁止滚动别名）", file=sys.stderr)
        return EXIT_CONFIG

    api_key = resolve_api_key(args) if provider != "fake" else ""
    base_url = resolve_base_url(args) if provider != "fake" else ""
    if provider != "fake" and not api_key:
        print(
            "错误：缺少 API 密钥。用 --api-key 传入，或设环境变量 REGRET_GATE_API_KEY / "
            f"{'OPENAI_API_KEY' if provider == 'openai' else 'ANTHROPIC_API_KEY'}",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    out_dir = args.out or paths.default_out_dir()
    os.makedirs(out_dir, exist_ok=True)
    started = time.monotonic()

    run_all(
        out_dir=out_dir,
        configs_dir=args.configs or paths.bundled_configs_dir(),
        task_limit=args.limit,
        runs_override=args.runs,
        groups=groups,
        verbose=not args.quiet_json,
        tasks_dir=paths.bundled_tasks_dir(),
        provider=provider,
        base_url=base_url or None,
        api_key=api_key or None,
        model=model or None,
    )

    rows = load_metrics(os.path.join(out_dir, "metrics.jsonl"))
    meta = {
        "provider": provider,
        "model": model,
        "base_url": base_url or "(default)",
        "temperature": args.temperature,
        "task_limit": args.limit,
        "runs_per_task": args.runs,
        "groups": groups,
        "seconds": round(time.monotonic() - started, 1),
        "note": "api key 不落盘；审计仅记录 model / base_url / request_id",
    }
    with open(os.path.join(out_dir, "run_meta.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2, sort_keys=True)

    report_path = os.path.join(out_dir, "report.md")
    write_report(rows, report_path, meta=meta)
    summary = {
        "runs": len(rows),
        "failures": sum(1 for r in rows if r.get("error")),
        "out_dir": os.path.abspath(out_dir),
        "report": os.path.abspath(report_path),
        "seconds": meta["seconds"],
    }
    print(json.dumps(summary, ensure_ascii=False))
    return EXIT_OK


# ---------------------------------------------------------------------------
# report / init / gui
# ---------------------------------------------------------------------------


def cmd_report(args) -> int:
    out_dir = args.out or paths.default_out_dir()
    metrics_path = os.path.join(out_dir, "metrics.jsonl")
    if not os.path.isfile(metrics_path):
        print(f"错误：找不到 {metrics_path}，请先运行 run", file=sys.stderr)
        return EXIT_CONFIG
    rows = load_metrics(metrics_path)
    meta_path = os.path.join(out_dir, "run_meta.json")
    meta = {}
    if os.path.isfile(meta_path):
        with open(meta_path, "r", encoding="utf-8") as handle:
            meta = json.load(handle)
    text = write_report(rows, os.path.join(out_dir, "report.md"), meta=meta)
    print(json.dumps({"runs": len(rows), "report": os.path.abspath(os.path.join(out_dir, "report.md")),
                      "chars": len(text)}, ensure_ascii=False))
    return EXIT_OK


def cmd_init(args) -> int:
    """在当前目录铺出可编辑的 configs 副本 + 说明文件，方便改配置后重跑。"""
    import shutil

    configs_src = paths.bundled_configs_dir()
    target = args.out or paths.default_out_dir()
    configs_dst = os.path.join(target, "configs")
    if os.path.exists(configs_dst) and not args.force:
        print(f"已存在：{configs_dst}（加 --force 覆盖）", file=sys.stderr)
        return EXIT_CONFIG
    shutil.copytree(configs_src, configs_dst, dirs_exist_ok=True)
    print(json.dumps({"configs": os.path.abspath(configs_dst)}, ensure_ascii=False))
    return EXIT_OK


def cmd_gui(args) -> int:
    try:
        from experiments.gui import launch
    except Exception as exc:  # noqa: BLE001
        print(f"图形界面不可用（{type(exc).__name__}: {exc}）", file=sys.stderr)
        print("请改用命令行：regret-gate-harness.exe run --provider fake --limit 4", file=sys.stderr)
        return EXIT_ERROR
    return launch()


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="regret-gate-harness",
        description=f"{APP_NAME} v{VERSION} · A–H 离线/真实 API 实验（FakeClient 或 openai/anthropic）",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="跑实验组并生成报告")
    run.add_argument("--provider", default="fake", choices=["fake", "openai", "anthropic"])
    run.add_argument("--model", default="", help="模型名（真实 provider 必填，写死版本）")
    run.add_argument("--base-url", dest="base_url", default="", help="API 地址")
    run.add_argument("--api-key", dest="api_key", default="", help="API 密钥（也可用环境变量）")
    run.add_argument("--group", default="all", help="A–H 逗号分隔，或 all")
    run.add_argument("--limit", type=int, default=20, help="每组任务数（按类别轮转取样）")
    run.add_argument("--runs", type=int, default=1, help="每任务重复次数")
    run.add_argument("--temperature", type=float, default=0.0)
    run.add_argument("--configs", default="", help="配置目录（默认用内置 configs）")
    run.add_argument("--out", default="", help="产出目录（默认 <exe目录>/output）")
    run.add_argument("--quiet-json", action="store_true", help="不逐 run 打印，只输出汇总 JSON")

    rep = sub.add_parser("report", help="从已有 metrics.jsonl 重新生成报告")
    rep.add_argument("--out", default="")

    init = sub.add_parser("init", help="把内置 configs 复制到可写目录以便修改")
    init.add_argument("--out", default="")
    init.add_argument("--force", action="store_true")

    selftest = sub.add_parser("selftest", help="离线自检（不联网）")
    selftest.add_argument("--work", default="")

    sub.add_parser("gui", help="打开图形启动器")
    return parser


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    # 不带参数：打包后直接开 GUI；源码模式打印帮助（避免开发机上意外弹窗）
    if not argv:
        if paths.is_frozen():
            return cmd_gui(argparse.Namespace())
        parser.print_help()
        return EXIT_OK

    args = parser.parse_args(argv)
    handler = {
        "run": cmd_run,
        "report": cmd_report,
        "init": cmd_init,
        "selftest": cmd_selftest,
        "gui": cmd_gui,
    }.get(args.command)
    if handler is None:
        parser.print_help()
        return EXIT_OK
    try:
        return handler(args)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，给出可读错误
        print(f"执行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
