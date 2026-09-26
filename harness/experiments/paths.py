"""资源与运行目录解析（源码运行 / PyInstaller 单文件 exe 双模式）。

打包成 `--onefile` 后，代码与 `configs/` 会被解包到临时目录（`sys._MEIPASS`），
**只读且随进程消失**；所有产出必须写到 exe 旁边的可写目录。本模块统一这两件事，
避免任何地方用相对路径去猜。
"""

from __future__ import annotations

import os
import sys


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> str:
    """可写根目录：exe 所在目录（打包后）或 harness/ 目录（源码运行）。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_root() -> str:
    """只读资源根目录（内含 configs/）。"""
    if is_frozen():
        return getattr(sys, "_MEIPASS", app_root())
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bundled_configs_dir() -> str:
    """打包/源码两种模式下 configs/ 的位置。"""
    candidate = os.path.join(resource_root(), "configs")
    if os.path.isdir(candidate):
        return candidate
    return os.path.join(app_root(), "configs")


def bundled_tasks_dir() -> str:
    return os.path.join(bundled_configs_dir(), "tasks")


def default_out_dir() -> str:
    """默认产出目录：可写根下的 output/。"""
    return os.path.join(app_root(), "output")


def default_log_path() -> str:
    return os.path.join(default_out_dir(), "launcher.log")


def ensure_writable_root() -> str:
    """确保可写根存在且可写；不可写时抛出带路径的明确错误。"""
    root = app_root()
    try:
        os.makedirs(root, exist_ok=True)
        probe = os.path.join(root, ".write_probe")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
    except OSError as exc:
        raise PermissionError(
            f"目录不可写：{root}（{exc}）。请把 exe 放到有写权限的目录，或用 --out 指定可写目录。"
        ) from exc
    return root


def describe_paths() -> dict:
    return {
        "frozen": is_frozen(),
        "app_root": app_root(),
        "resource_root": resource_root(),
        "configs_dir": bundled_configs_dir(),
        "tasks_dir": bundled_tasks_dir(),
        "default_out_dir": default_out_dir(),
        "executable": sys.executable,
    }
