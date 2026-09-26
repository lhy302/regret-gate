"""GUI 冒烟测试：窗口能不能真的建起来（无头/无显示环境自动跳过）。

不点按钮、不跑实验，只验证：
- Tk 能初始化；
- 三个字段（API 地址 / 密钥 / 模型）控件存在且可读写；
- provider 切换会把地址与模型预填、并给出默认地址；
- 消息队列轮询不抛异常。

这能挡住「打包后 GUI 一启动就崩」这类只靠单测看不出来的问题。
"""

from __future__ import annotations

import unittest


def _tk_available() -> tuple:
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.destroy()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


AVAILABLE, REASON = _tk_available()


@unittest.skipUnless(AVAILABLE, f"无可用的图形环境：{REASON}")
class GuiSmokeTest(unittest.TestCase):
    def setUp(self):
        from experiments.gui import LauncherApp

        self.app = LauncherApp()
        self.app.root.withdraw()

    def tearDown(self):
        try:
            self.app.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def test_three_fields_present(self):
        self.assertTrue(hasattr(self.app, "base_url"))
        self.assertTrue(hasattr(self.app, "api_key"))
        self.assertTrue(hasattr(self.app, "model"))
        self.assertEqual(self.app.limit.get(), "20")

    def test_default_provider_is_offline_and_key_field_disabled(self):
        self.assertEqual(self.app._provider(), "fake")
        self.assertEqual(self.app.model.get(), "fake-model-v1")
        self.assertEqual(str(self.app.key_entry.cget("state")), "disabled")

    def test_switching_to_openai_prefills_url_and_models(self):
        self.app.provider.set("openai（OpenAI 兼容端点）")
        self.app._on_provider_change()
        self.assertIn("openai.com", self.app.base_url.get())
        self.assertEqual(str(self.app.key_entry.cget("state")), "normal")
        values = self.app.model_box.cget("values")
        self.assertTrue(len(values) >= 1)
        self.assertTrue(all(isinstance(v, str) for v in values))

    def test_switching_to_anthropic_prefills_its_url(self):
        self.app.provider.set("anthropic（Claude）")
        self.app._on_provider_change()
        self.assertIn("anthropic.com", self.app.base_url.get())
        self.assertIn("claude", self.app.model.get())

    def test_collect_reads_widget_state(self):
        self.app.provider.set("fake（离线桩，不需要密钥）")
        self.app._on_provider_change()
        self.app.limit.set("4")
        self.app.runs.set("2")
        cfg = self.app._collect()
        self.assertEqual(cfg["provider"], "fake")
        self.assertEqual(cfg["limit"], 4)
        self.assertEqual(cfg["runs"], 2)
        self.assertEqual(cfg["groups"], ["A", "B", "C", "D", "E", "F", "G", "H"])

    def test_validation_requires_key_for_real_provider(self):
        self.app.provider.set("openai（OpenAI 兼容端点）")
        self.app._on_provider_change()
        self.app.api_key.set("")
        problem = self.app._validate(self.app._collect())
        self.assertIsNotNone(problem)
        self.assertIn("密钥", problem)
        self.app.api_key.set("sk-x")
        self.app.model.set("m")
        self.assertIsNone(self.app._validate(self.app._collect()))

    def test_validation_requires_at_least_one_group(self):
        for var in self.app.group_vars.values():
            var.set(False)
        problem = self.app._validate(self.app._collect())
        self.assertIn("实验组", problem)

    def test_poll_messages_does_not_raise(self):
        self.app.messages.put(("log", "hello"))
        self.app.messages.put(("status", "ok"))
        self.app.messages.put(("done", {"returncode": 0, "seconds": 1.0, "out_dir": "."}))
        self.app._poll_messages()
        self.assertIn("hello", self.app.log.get("1.0", "end"))


if __name__ == "__main__":
    unittest.main()
