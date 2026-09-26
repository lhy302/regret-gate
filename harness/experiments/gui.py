"""图形启动器（tkinter，标准库，打包后无需额外依赖）。

设计要点：

- **三个字段**：API 地址 / API 密钥 / 模型名称；密钥填完可点「获取模型列表」从
  `/models` 端点拉取候选（OpenAI 兼容与 Anthropic 都支持）。
- **密钥处理**：默认不落盘，仅在启动子进程时通过**环境变量**传递；
  勾选「记住密钥」才写入本机 `output/launcher_config.json`，并且文件里标明风险。
- **子进程无控制台**：进度与日志走 `experiments/job_runner.py`，输出重定向到日志文件，
  GUI 只负责把日志尾部显示出来（打包成 windowed exe 后没有 stdout 可用）。
- 所有耗时操作（拉模型列表、跑实验、自检）都在后台线程，UI 不卡死；
  线程通过 `queue` 把消息回传主线程，tkinter 调用只发生在主线程。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import traceback
import webbrowser
from typing import Optional

from experiments import paths
from experiments.job_runner import run_job, run_selftest
from llm.model_list import ModelListError, list_models, provider_defaults

APP_TITLE = "后悔承诺门 · Harness 启动器"
CONFIG_FILENAME = "launcher_config.json"

PROVIDERS = ["fake", "openai", "anthropic"]
PROVIDER_LABELS = {
    "fake": "fake（离线桩，不需要密钥）",
    "openai": "openai（OpenAI 兼容端点）",
    "anthropic": "anthropic（Claude）",
}
GROUP_CHOICES = ["A", "B", "C", "D", "E", "F", "G", "H"]

SUGGESTED_MODELS = {
    "openai": ["gpt-4o-2024-08-06", "gpt-4o-mini-2024-07-18"],
    "anthropic": ["claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022"],
    "fake": ["fake-model-v1"],
}


def config_path() -> str:
    return os.path.join(paths.default_out_dir(), CONFIG_FILENAME)


def load_config() -> dict:
    path = config_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(data: dict) -> None:
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class LauncherApp:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = tk.Tk()
        self.root.title(f"{APP_TITLE} v1.0.0")
        self.root.geometry("980x720")
        self.root.minsize(880, 640)

        self.messages: "queue.Queue[tuple]" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.start_time = 0.0
        self.saved = load_config()

        self._build_ui()
        self._apply_saved()
        self._poll_messages()

    # -- UI 构建 ----------------------------------------------------------

    def _build_ui(self) -> None:
        tk, ttk = self.tk, self.ttk
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        # 顶部：API 配置
        api = ttk.LabelFrame(outer, text="① API 配置（填地址与密钥后可点右侧按钮拉取模型列表）", padding=10)
        api.pack(fill="x")
        api.columnconfigure(1, weight=1)

        ttk.Label(api, text="提供方").grid(row=0, column=0, sticky="w", pady=4)
        self.provider = tk.StringVar(value="fake")
        provider_box = ttk.Combobox(
            api, textvariable=self.provider, state="readonly",
            values=[PROVIDER_LABELS[p] for p in PROVIDERS], width=28,
        )
        provider_box.grid(row=0, column=1, sticky="w", pady=4)
        provider_box.bind("<<ComboboxSelected>>", lambda _e: self._on_provider_change())

        ttk.Label(api, text="API 地址").grid(row=1, column=0, sticky="w", pady=4)
        self.base_url = tk.StringVar(value="")
        ttk.Entry(api, textvariable=self.base_url).grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(api, text="API 密钥").grid(row=2, column=0, sticky="w", pady=4)
        self.api_key = tk.StringVar(value="")
        self.key_entry = ttk.Entry(api, textvariable=self.api_key, show="•")
        self.key_entry.grid(row=2, column=1, sticky="ew", pady=4)
        self.show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            api, text="显示", variable=self.show_key, command=self._toggle_key
        ).grid(row=2, column=2, sticky="w", padx=(6, 0))
        self.remember_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            api, text="记住密钥（写入本机 output/launcher_config.json）", variable=self.remember_key
        ).grid(row=3, column=1, sticky="w", pady=2)

        ttk.Label(api, text="模型名称").grid(row=4, column=0, sticky="w", pady=4)
        self.model = tk.StringVar(value="fake-model-v1")
        self.model_box = ttk.Combobox(api, textvariable=self.model, values=SUGGESTED_MODELS["fake"])
        self.model_box.grid(row=4, column=1, sticky="ew", pady=4)
        self.fetch_btn = ttk.Button(api, text="获取模型列表", command=self._on_fetch_models)
        self.fetch_btn.grid(row=4, column=2, sticky="w", padx=(6, 0))

        self.hint = ttk.Label(api, text="", foreground="#666")
        self.hint.grid(row=5, column=1, sticky="w")

        # 中部：实验参数
        run_box = ttk.LabelFrame(outer, text="② 实验参数", padding=10)
        run_box.pack(fill="x", pady=(10, 0))
        run_box.columnconfigure(6, weight=1)

        ttk.Label(run_box, text="实验组").grid(row=0, column=0, sticky="w")
        self.group_vars = {}
        inner = ttk.Frame(run_box)
        inner.grid(row=0, column=1, columnspan=7, sticky="w", padx=(6, 0))
        for group in GROUP_CHOICES:
            var = self.tk.BooleanVar(value=True)
            self.group_vars[group] = var
            ttk.Checkbutton(inner, text=group, variable=var).pack(side="left")

        ttk.Label(run_box, text="每组任务数").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.limit = self.tk.StringVar(value="20")
        ttk.Spinbox(run_box, from_=1, to=80, width=6, textvariable=self.limit).grid(
            row=1, column=1, sticky="w", pady=(8, 0)
        )
        ttk.Label(run_box, text="每任务重复").grid(row=1, column=2, sticky="e", pady=(8, 0))
        self.runs = self.tk.StringVar(value="1")
        ttk.Spinbox(run_box, from_=1, to=10, width=6, textvariable=self.runs).grid(
            row=1, column=3, sticky="w", pady=(8, 0)
        )
        ttk.Label(run_box, text="产出目录").grid(row=1, column=4, sticky="e", pady=(8, 0))
        self.out_dir = self.tk.StringVar(value=paths.default_out_dir())
        ttk.Entry(run_box, textvariable=self.out_dir).grid(
            row=1, column=5, columnspan=3, sticky="ew", pady=(8, 0), padx=(6, 0)
        )

        # 按钮
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(10, 0))
        self.run_btn = ttk.Button(buttons, text="开始实验", command=self._on_run)
        self.run_btn.pack(side="left")
        self.selftest_btn = ttk.Button(buttons, text="离线自检", command=self._on_selftest)
        self.selftest_btn.pack(side="left", padx=6)
        ttk.Button(buttons, text="打开产出目录", command=self._open_out_dir).pack(side="left", padx=6)
        ttk.Button(buttons, text="保存配置", command=self._on_save_config).pack(side="left", padx=6)
        self.status = ttk.Label(buttons, text="就绪", foreground="#0a6")
        self.status.pack(side="right")

        # 日志
        log_box = ttk.LabelFrame(outer, text="③ 运行日志", padding=6)
        log_box.pack(fill="both", expand=True, pady=(10, 0))
        self.log = self.tk.Text(log_box, height=18, wrap="none")
        self.log.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(log_box, orient="vertical", command=self.log.yview)
        scroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scroll.set)

        self._append(f"{APP_TITLE} v1.0.0")
        self._append(f"可写根目录：{paths.app_root()}")
        self._append(f"内置配置：{paths.bundled_configs_dir()}")
        self._append(f"内置任务集：{paths.bundled_tasks_dir()}")
        self._append("")
        self._append("提示：离线 provider=fake 不需要密钥，可直接点「开始实验」验证链路。")
        self._append("      真实 API 需填地址+密钥+模型名；模型名可点「获取模型列表」自动拉取。")

    def _toggle_key(self) -> None:
        self.key_entry.configure(show="" if self.show_key.get() else "•")

    def _on_provider_change(self) -> None:
        provider = self._provider()
        defaults = provider_defaults(provider)
        self.base_url.set(defaults["base_url"])
        self.hint.configure(text=defaults["hint"])
        self.model_box.configure(values=SUGGESTED_MODELS.get(provider, []))
        self.model.set(SUGGESTED_MODELS.get(provider, [""])[0])
        state = "disabled" if provider == "fake" else "normal"
        self.fetch_btn.configure(state=state)
        self.key_entry.configure(state=state)

    def _provider(self) -> str:
        raw = self.provider.get()
        for key, label in PROVIDER_LABELS.items():
            if raw == label:
                return key
        return "fake"

    def _apply_saved(self) -> None:
        saved = self.saved
        if not saved:
            self._on_provider_change()
            return
        provider = saved.get("provider", "fake")
        self.provider.set(PROVIDER_LABELS.get(provider, PROVIDER_LABELS["fake"]))
        self.base_url.set(saved.get("base_url", ""))
        self.model.set(saved.get("model", "fake-model-v1"))
        if saved.get("remember_key") and saved.get("api_key"):
            self.api_key.set(saved["api_key"])
            self.remember_key.set(True)
        limits = saved.get("limit")
        if limits:
            self.limit.set(str(limits))
        runs = saved.get("runs")
        if runs:
            self.runs.set(str(runs))
        if saved.get("out_dir"):
            self.out_dir.set(saved["out_dir"])
        groups = saved.get("groups")
        if groups:
            for group, var in self.group_vars.items():
                var.set(group in groups)
        self._on_provider_change()
        if saved.get("model"):
            self.model.set(saved["model"])
        self._append("已载入上次保存的配置。")

    # -- 日志 -------------------------------------------------------------

    def _append(self, text: str) -> None:
        self.log.insert("end", text + "\n")
        self.log.see("end")

    def _poll_messages(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self._append(payload)
                elif kind == "status":
                    self.status.configure(text=payload)
                elif kind == "models":
                    self._set_models(payload)
                elif kind == "done":
                    self._on_job_done(payload)
                elif kind == "error":
                    self._append(f"[错误] {payload}")
        except queue.Empty:
            pass
        self.root.after(200, self._poll_messages)

    def _set_models(self, models: list) -> None:
        self.model_box.configure(values=models)
        if models:
            current = self.model.get()
            if current not in models:
                self.model.set(models[0])
        self._append(f"已获取 {len(models)} 个模型。")
        self.status.configure(text="已获取模型列表", foreground="#0a6")

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.run_btn.configure(state=state)
        self.selftest_btn.configure(state=state)
        self.fetch_btn.configure(state=state if self._provider() != "fake" else "disabled")

    def _on_job_done(self, info: dict) -> None:
        self._set_busy(False)
        code = info.get("returncode")
        ok = code == 0
        self.status.configure(
            text=f"完成（exit={code}，{info.get('seconds', '?')}s）" if ok else f"失败（exit={code}）",
            foreground="#0a6" if ok else "#c33",
        )
        self._append(f"--- 结束 exit={code} 用时 {info.get('seconds', '?')}s ---")
        log_path = info.get("log_path")
        if log_path and os.path.isfile(log_path):
            self._append(f"完整日志：{log_path}")
            self._tail_log(log_path)
        report = os.path.join(info.get("out_dir", ""), "report.md")
        if ok and os.path.isfile(report):
            self._append(f"报告：{report}")
            self.status.configure(text=f"完成，报告已生成：{report}", foreground="#0a6")

    def _tail_log(self, path: str, lines: int = 40) -> None:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.readlines()
            for line in content[-lines:]:
                self._append(line.rstrip())
        except OSError as exc:
            self._append(f"[无法读取日志] {exc}")

    # -- 动作 -------------------------------------------------------------

    def _collect(self) -> dict:
        groups = [g for g, var in self.group_vars.items() if var.get()]
        return {
            "provider": self._provider(),
            "base_url": self.base_url.get().strip(),
            "api_key": self.api_key.get().strip(),
            "model": self.model.get().strip(),
            "groups": groups,
            "limit": int(self.limit.get() or 20),
            "runs": int(self.runs.get() or 1),
            "out_dir": self.out_dir.get().strip() or paths.default_out_dir(),
        }

    def _validate(self, cfg: dict) -> Optional[str]:
        if not cfg["groups"]:
            return "请至少勾选一个实验组"
        if cfg["provider"] != "fake":
            if not cfg["base_url"]:
                return "真实 provider 需要填写 API 地址"
            if not cfg["api_key"]:
                return "真实 provider 需要填写 API 密钥"
            if not cfg["model"]:
                return "真实 provider 需要填写或选择模型名称"
        return None

    def _on_fetch_models(self) -> None:
        cfg = self._collect()
        if cfg["provider"] == "fake":
            self._append("离线 provider 不需要模型列表。")
            return
        if not cfg["base_url"] or not cfg["api_key"]:
            self._append("[提示] 请先填写 API 地址与密钥。")
            return
        self._set_busy(True)
        self.status.configure(text="正在获取模型列表…", foreground="#666")
        self._append(f"请求模型列表：{cfg['base_url']}（密钥不进日志）")

        def work() -> None:
            try:
                models = list_models(cfg["base_url"], cfg["api_key"], provider=cfg["provider"])
                self.messages.put(("models", models))
            except ModelListError as exc:
                self.messages.put(("error", f"获取模型列表失败：{exc}"))
                self.messages.put(("status", "获取失败"))
            except Exception as exc:  # noqa: BLE001
                self.messages.put(("error", f"获取模型列表异常：{type(exc).__name__}: {exc}"))
            finally:
                self.messages.put(("done", {"returncode": 0, "seconds": 0, "out_dir": cfg["out_dir"]}))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _on_selftest(self) -> None:
        self._set_busy(True)
        self.status.configure(text="正在自检…", foreground="#666")
        self._append("--- 离线自检开始（不联网）---")

        def work() -> None:
            try:
                info = run_selftest()
                self.messages.put(("done", info))
            except Exception as exc:  # noqa: BLE001
                self.messages.put(("error", f"{type(exc).__name__}: {exc}"))
                self.messages.put(("done", {"returncode": 1, "seconds": 0, "out_dir": paths.default_out_dir()}))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _on_run(self) -> None:
        cfg = self._collect()
        problem = self._validate(cfg)
        if problem:
            self._append(f"[提示] {problem}")
            self.status.configure(text=problem, foreground="#c33")
            return
        if cfg["provider"] != "fake" and not self.remember_key.get():
            self._append("（密钥仅本次使用，不会写入配置文件）")

        self._set_busy(True)
        self.start_time = time.monotonic()
        self.status.configure(text="运行中…", foreground="#666")
        self._append("")
        self._append(
            f"--- 开始：provider={cfg['provider']} model={cfg['model'] or '(fake 默认)'} "
            f"groups={','.join(cfg['groups'])} limit={cfg['limit']} runs={cfg['runs']} ---"
        )

        def tick(elapsed: float) -> None:
            self.messages.put(("status", f"运行中… {elapsed:.0f}s"))

        def work() -> None:
            try:
                info = run_job(
                    provider=cfg["provider"],
                    model=cfg["model"] or "fake-model-v1",
                    groups=",".join(cfg["groups"]),
                    limit=cfg["limit"],
                    runs=cfg["runs"],
                    api_key=cfg["api_key"],
                    base_url=cfg["base_url"],
                    out_dir=cfg["out_dir"],
                    on_tick=tick,
                )
                self.messages.put(("done", info))
            except Exception as exc:  # noqa: BLE001
                self.messages.put(("error", f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"))
                self.messages.put(("done", {"returncode": 1, "seconds": 0, "out_dir": cfg["out_dir"]}))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _on_save_config(self) -> None:
        cfg = self._collect()
        remember = bool(self.remember_key.get())
        data = {
            "provider": cfg["provider"],
            "base_url": cfg["base_url"],
            "model": cfg["model"],
            "groups": cfg["groups"],
            "limit": cfg["limit"],
            "runs": cfg["runs"],
            "out_dir": cfg["out_dir"],
            "remember_key": remember,
        }
        if remember and cfg["api_key"]:
            data["api_key"] = cfg["api_key"]
        save_config(data)
        self._append(f"配置已保存：{config_path()}" + ("（含密钥，请注意该文件权限）" if remember else "（未保存密钥）"))

    def _open_out_dir(self) -> None:
        path = self.out_dir.get().strip() or paths.default_out_dir()
        os.makedirs(path, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                webbrowser.open(f"file://{path}")
        except OSError as exc:
            self._append(f"[无法打开目录] {exc}")

    def run(self) -> int:
        self.root.mainloop()
        return 0


def launch() -> int:
    """打开启动器。tkinter 缺失时抛出，由 CLI 层给出命令行替代方案。"""
    import importlib.util

    if importlib.util.find_spec("tkinter") is None:
        raise RuntimeError("当前 Python 未包含 tkinter（图形界面不可用）")

    app = LauncherApp()
    return app.run()
