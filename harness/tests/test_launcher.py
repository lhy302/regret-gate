"""启动器与打包适配的验收测试（GUI / job_runner / paths / model_list）。

重点锁住三件在 Windows 打包场景下最容易翻车的事：

1. **API 密钥绝不进命令行**（命令行对其他进程可见），只走环境变量；
2. 子进程必须以「无窗口 + 输出重定向到日志文件」方式启动 —— windowed exe 没有 stdout，
   用管道或直接 print 会直接崩；
3. 冻结模式下 `sys.executable` 是 exe 自己，不能再当 Python 解释器去跑单测文件。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from experiments import job_runner, paths
from llm import model_list
from llm.model_list import ModelListError, list_models, provider_defaults

FAKE_KEY = "sk-THIS-MUST-NEVER-APPEAR-IN-ARGV"


class PathsTest(unittest.TestCase):
    def test_describe_paths_has_required_keys(self):
        info = paths.describe_paths()
        for key in ("frozen", "app_root", "resource_root", "configs_dir", "tasks_dir", "default_out_dir"):
            self.assertIn(key, info)

    def test_configs_dir_exists_in_source_mode(self):
        self.assertTrue(os.path.isdir(paths.bundled_configs_dir()), paths.bundled_configs_dir())
        self.assertTrue(os.path.isdir(paths.bundled_tasks_dir()), paths.bundled_tasks_dir())

    def test_default_out_dir_sits_under_app_root(self):
        out = paths.default_out_dir()
        self.assertTrue(out.startswith(paths.app_root()), f"{out} 不在 {paths.app_root()} 下")

    def test_not_frozen_when_running_from_source(self):
        self.assertFalse(paths.is_frozen())
        self.assertEqual(paths.resource_root(), paths.app_root())


class BuildInvocationTest(unittest.TestCase):
    def test_argv_never_contains_api_key(self):
        argv, env_extra = job_runner.build_invocation(
            provider="openai", model="gpt-4o-2024-08-06", groups="A,F",
            limit=4, runs=1, out_dir="out", configs_dir="C:/cfg",
        )
        joined = " ".join(argv)
        self.assertNotIn(FAKE_KEY, joined)
        self.assertNotIn("--api-key", argv, "密钥不允许走命令行参数")
        self.assertIn("run", argv)
        self.assertIn("--provider", argv)
        self.assertIn("openai", argv)
        self.assertIn("--configs", argv)
        self.assertIn("C:/cfg", argv)
        self.assertNotIn(FAKE_KEY, json.dumps(env_extra))

    def test_run_job_always_passes_bundled_configs(self):
        """真实运行时必须显式带上 configs 路径，不能靠工作目录猜（打包后必崩）。"""
        argv, _ = job_runner.build_invocation(
            provider="fake", model="fake-model-v1", groups="A", limit=1, runs=1,
            out_dir="out", configs_dir=paths.bundled_configs_dir(),
        )
        self.assertIn("--configs", argv)
        self.assertIn(paths.bundled_configs_dir(), argv)

    def test_invocation_targets_self_when_frozen(self):
        argv, _ = job_runner.build_invocation(
            provider="fake", model="fake-model-v1", groups="all",
            limit=2, runs=1, out_dir="out",
        )
        self.assertEqual(argv[0], sys.executable)
        if not paths.is_frozen():
            self.assertTrue(argv[1].endswith("harness_cli.py"))

    def test_build_env_puts_key_in_environment_only(self):
        env = job_runner.build_env(FAKE_KEY, "https://example.invalid/v1", {"REGRET_GATE_PROVIDER": "openai"})
        self.assertEqual(env[job_runner.ENV_API_KEY], FAKE_KEY)
        self.assertEqual(env[job_runner.ENV_BASE_URL], "https://example.invalid/v1")
        self.assertEqual(env["OPENAI_BASE_URL"], "https://example.invalid/v1")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["REGRET_GATE_PROVIDER"], "openai")

    def test_empty_key_leaves_no_trace(self):
        env = job_runner.build_env("", "")
        self.assertNotIn(job_runner.ENV_API_KEY, env)
        self.assertNotIn(job_runner.ENV_BASE_URL, env)

    def test_no_window_flags_on_windows(self):
        kwargs = job_runner._no_window_kwargs()
        if os.name == "nt":
            self.assertIn("creationflags", kwargs)
            self.assertIn("startupinfo", kwargs)
            self.assertEqual(kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        else:
            self.assertEqual(kwargs, {})


class ModelListTest(unittest.TestCase):
    def setUp(self):
        self.original = model_list._request

    def tearDown(self):
        model_list._request = self.original

    def _patch(self, payload: bytes, status: int = 200):
        def fake_request(url, headers, timeout):
            fake_request.url = url
            fake_request.headers = headers
            return status, payload
        model_list._request = fake_request
        return fake_request

    def test_openai_style_model_list_parsed_and_sorted(self):
        request = self._patch(json.dumps(
            {"object": "list", "data": [{"id": "m-b"}, {"id": "m-a"}, {"id": "m-a"}]}
        ).encode("utf-8"))
        models = list_models("https://api.example.com/v1", "sk-x", provider="openai")
        self.assertEqual(models, ["m-a", "m-b"])
        self.assertTrue(request.url.endswith("/v1/models"), request.url)
        self.assertEqual(request.headers["Authorization"], "Bearer sk-x")

    def test_base_url_without_v1_gets_v1_appended(self):
        request = self._patch(json.dumps({"data": [{"id": "m"}]}).encode("utf-8"))
        list_models("https://api.example.com", "sk-x", provider="openai")
        self.assertTrue(request.url.endswith("/v1/models"), request.url)

    def test_anthropic_uses_api_key_header(self):
        request = self._patch(json.dumps({"data": [{"id": "claude-x"}]}).encode("utf-8"))
        models = list_models("https://api.anthropic.com/v1", "sk-ant", provider="anthropic")
        self.assertEqual(models, ["claude-x"])
        self.assertEqual(request.headers["x-api-key"], "sk-ant")
        self.assertNotIn("Authorization", request.headers)

    def test_models_as_plain_strings_supported(self):
        self._patch(json.dumps({"models": ["b", "a"]}).encode("utf-8"))
        self.assertEqual(list_models("https://h/v1", "k"), ["a", "b"])

    def test_empty_key_rejected(self):
        with self.assertRaises(ModelListError):
            list_models("https://h/v1", "")

    def test_bad_scheme_rejected(self):
        with self.assertRaises(ModelListError):
            list_models("api.example.com/v1", "k")

    def test_http_error_surfaces_message(self):
        def boom(url, headers, timeout):
            raise ModelListError("HTTP 401: bad key")
        model_list._request = boom
        with self.assertRaises(ModelListError) as ctx:
            list_models("https://h/v1", "k")
        self.assertIn("401", str(ctx.exception))

    def test_non_json_response_rejected(self):
        self._patch(b"<html>not json</html>")
        with self.assertRaises(ModelListError) as ctx:
            list_models("https://h/v1", "k")
        self.assertIn("JSON", str(ctx.exception))

    def test_success_without_models_is_an_error_not_empty_silence(self):
        self._patch(json.dumps({"data": []}).encode("utf-8"))
        with self.assertRaises(ModelListError):
            list_models("https://h/v1", "k")

    def test_provider_defaults(self):
        self.assertIn("openai.com", provider_defaults("openai")["base_url"])
        self.assertIn("anthropic.com", provider_defaults("anthropic")["base_url"])
        self.assertEqual(provider_defaults("fake")["base_url"], "")


class FrozenValidatorTest(unittest.TestCase):
    """冻结模式下不能拿 sys.executable 当解释器。"""

    def test_python_executable_is_none_when_frozen(self):
        from experiments import validators

        original = getattr(sys, "frozen", None)
        try:
            sys.frozen = True  # type: ignore[attr-defined]
            self.assertIsNone(validators._python_executable())
            self.assertFalse(validators._can_spawn_subprocess())
        finally:
            if original is None:
                delattr(sys, "frozen")
            else:
                sys.frozen = original  # type: ignore[attr-defined]

    def test_unit_test_returns_unavailable_when_frozen(self):
        from experiments import validators
        from core.types import Task, ValidationSpec

        original = getattr(sys, "frozen", None)
        try:
            sys.frozen = True  # type: ignore[attr-defined]
            task = Task(
                id="t", category="long_code", prompt="p",
                validation=ValidationSpec(kind="unit_test", config={"test_file": __file__}),
            )
            result = validators.validate_task_output(task, "print(1)\n")
            self.assertFalse(result["passed"])
            self.assertEqual(result["details"]["status"], "unavailable")
            self.assertFalse(result["syntax_error"])
        finally:
            if original is None:
                delattr(sys, "frozen")
            else:
                sys.frozen = original  # type: ignore[attr-defined]


class GuiModuleTest(unittest.TestCase):
    def test_gui_module_imports_and_exposes_launch(self):
        from experiments import gui

        self.assertTrue(callable(gui.launch))
        self.assertEqual(gui.PROVIDERS, ["fake", "openai", "anthropic"])

    def test_config_roundtrip_never_writes_key_unless_remembered(self):
        from experiments import gui

        path = gui.config_path()
        backup = None
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as handle:
                backup = handle.read()
        try:
            gui.save_config({"provider": "openai", "base_url": "https://x/v1", "model": "m",
                             "remember_key": False})
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertNotIn("api_key", data)
            self.assertEqual(data["model"], "m")
        finally:
            if backup is None:
                if os.path.isfile(path):
                    os.remove(path)
            else:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(backup)


class CliParserTest(unittest.TestCase):
    def test_parser_has_all_subcommands(self):
        import harness_cli

        parser = harness_cli.build_parser()
        subparsers = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
        names = set()
        for action in subparsers:
            names |= set(action.choices.keys())
        self.assertEqual(names, {"run", "report", "init", "selftest", "gui"})

    def test_parse_groups_accepts_all_and_lists(self):
        import harness_cli

        self.assertEqual(harness_cli.parse_groups("all"), harness_cli.GROUP_ORDER)
        self.assertEqual(harness_cli.parse_groups("a,f"), ["A", "F"])
        with self.assertRaises(SystemExit):
            harness_cli.parse_groups("Z")

    def test_api_key_resolution_precedence(self):
        import argparse
        import harness_cli

        args = argparse.Namespace(api_key="explicit", provider="openai")
        old = os.environ.pop("REGRET_GATE_API_KEY", None)
        old_openai = os.environ.pop("OPENAI_API_KEY", None)
        try:
            self.assertEqual(harness_cli.resolve_api_key(args), "explicit")
            args.api_key = ""
            os.environ["REGRET_GATE_API_KEY"] = "from-env"
            self.assertEqual(harness_cli.resolve_api_key(args), "from-env")
            del os.environ["REGRET_GATE_API_KEY"]
            os.environ["OPENAI_API_KEY"] = "from-provider-env"
            self.assertEqual(harness_cli.resolve_api_key(args), "from-provider-env")
        finally:
            os.environ.pop("REGRET_GATE_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            if old:
                os.environ["REGRET_GATE_API_KEY"] = old
            if old_openai:
                os.environ["OPENAI_API_KEY"] = old_openai


if __name__ == "__main__":
    unittest.main()
