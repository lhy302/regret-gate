"""启动器子进程调用封装（GUI 与命令行共用）。

关键约束（打包成 windowed exe 后没有 stdout/stderr，直接 print 会崩）：
- 一律以 `CREATE_NO_WINDOW` 启动子进程；
- 子进程 stdout/stderr 重定向到日志文件，而不是管道；
- API key 只经**环境变量**传给子进程，绝不进命令行（命令行对其他进程可见）。

`build_invocation()` 是纯函数，便于单测；`run_job()` 负责实际执行与日志落盘。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Optional

from experiments.paths import app_root, bundled_configs_dir, default_out_dir, is_frozen

# 环境变量名（子进程 argv 里看不到密钥）
ENV_API_KEY = "REGRET_GATE_API_KEY"
ENV_BASE_URL = "REGRET_GATE_BASE_URL"
ENV_PROVIDER = "REGRET_GATE_PROVIDER"
ENV_MODEL = "REGRET_GATE_MODEL"


def build_invocation(
    provider: str,
    model: str,
    groups: str,
    limit: int,
    runs: int,
    out_dir: str,
    configs_dir: Optional[str] = None,
) -> tuple:
    """构造 `(argv, env_extra)`。

    - argv 里**绝不包含** api_key；
    - 冻结模式下 argv[0] 是 exe 自己，源码模式下是 `python harness_cli.py`。
    """
    if is_frozen():
        argv = [sys.executable]
    else:
        entry = os.path.join(app_root(), "harness_cli.py")
        argv = [sys.executable, entry]
    argv += [
        "run",
        "--provider",
        provider,
        "--model",
        model,
        "--group",
        groups,
        "--limit",
        str(int(limit)),
        "--runs",
        str(int(runs)),
        "--out",
        out_dir,
        "--quiet-json",
    ]
    if configs_dir:
        argv += ["--configs", configs_dir]

    env_extra = {
        ENV_PROVIDER: provider,
        ENV_MODEL: model,
    }
    return argv, env_extra


def build_env(api_key: str, base_url: str, extra: Optional[dict] = None) -> dict:
    env = dict(os.environ)
    # 让子进程输出 UTF-8，避免中文任务集在 GBK 控制台下乱码
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if api_key:
        env[ENV_API_KEY] = api_key
    if base_url:
        env[ENV_BASE_URL] = base_url
        if not env.get("OPENAI_BASE_URL"):
            env["OPENAI_BASE_URL"] = base_url
        if not env.get("ANTHROPIC_BASE_URL"):
            env["ANTHROPIC_BASE_URL"] = base_url
    env.update(extra or {})
    return env


def _no_window_kwargs() -> dict:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
    }


def run_job(
    provider: str,
    model: str,
    groups: str,
    limit: int,
    runs: int,
    api_key: str = "",
    base_url: str = "",
    out_dir: Optional[str] = None,
    configs_dir: Optional[str] = None,
    log_path: Optional[str] = None,
    on_tick=None,
    timeout: Optional[float] = None,
) -> dict:
    """同步执行一次实验任务，返回 `{returncode, log_path, out_dir, seconds}`。

    `on_tick(elapsed_seconds)` 每 0.5s 回调一次，供 GUI 刷新进度（不阻塞主线程的是调用方）。
    """
    out_dir = out_dir or default_out_dir()
    os.makedirs(out_dir, exist_ok=True)
    log_path = log_path or os.path.join(out_dir, "run.log")
    configs_dir = configs_dir or bundled_configs_dir()

    argv, env_extra = build_invocation(
        provider=provider,
        model=model,
        groups=groups,
        limit=limit,
        runs=runs,
        out_dir=out_dir,
        configs_dir=configs_dir,
    )
    env = build_env(api_key, base_url, env_extra)

    started = time.monotonic()
    with open(log_path, "a", encoding="utf-8", newline="\n") as log:
        log.write(
            f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} start "
            f"provider={provider} model={model} groups={groups} limit={limit} runs={runs} ===\n"
        )
        log.flush()
        proc = subprocess.Popen(
            argv,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            cwd=app_root(),
            **_no_window_kwargs(),
        )
        while True:
            try:
                proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - started
                if on_tick:
                    on_tick(elapsed)
                if timeout and elapsed > timeout:
                    proc.kill()
                    log.write(f"\n[killed] 超过 {timeout:.0f}s 上限\n")
                    break
        log.write(f"=== exit={proc.returncode} seconds={time.monotonic() - started:.1f} ===\n")

    return {
        "returncode": proc.returncode,
        "log_path": log_path,
        "out_dir": out_dir,
        "seconds": round(time.monotonic() - started, 1),
    }


def run_selftest(log_path: Optional[str] = None, out_dir: Optional[str] = None) -> dict:
    """调用同一 exe 的 `selftest` 子命令（不需要密钥，验证打包完整性）。"""
    if is_frozen():
        argv = [sys.executable]
    else:
        argv = [sys.executable, os.path.join(app_root(), "harness_cli.py")]
    out_dir = out_dir or default_out_dir()
    os.makedirs(out_dir, exist_ok=True)
    argv += ["selftest", "--work", out_dir]
    env = build_env("", "")
    log_path = log_path or os.path.join(out_dir, "selftest.log")
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    with open(log_path, "a", encoding="utf-8", newline="\n") as log:
        proc = subprocess.run(
            argv,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            cwd=app_root(),
            timeout=600,
            **_no_window_kwargs(),
        )
    return {"returncode": proc.returncode, "log_path": log_path, "out_dir": out_dir, "seconds": 0}
