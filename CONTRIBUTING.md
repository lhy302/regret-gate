# 贡献指南

感谢愿意看这个项目。它是一个**实验性探针项目**，不是产品；改动前请先读懂下面三条红线。

---

## 三条红线（改代码前必读）

1. **不改动 API 厂商的缓存行为**，不假设前缀缓存可复用、不假设 provider 支持局部重算。
   上下文变了就是全量重算，这是默认事实，不是优化点。
2. **不碰 KV cache / logits / hidden states / attention**。本 harness 只操作 `messages`
   与字符串。相关机制（KV 局部复用、从编辑点重算、下游偏移蒸发、前缀缓存失效）
   已按 v0.2 §0.1 **移出实验范围**，不要把它们重新引入。
3. **不依赖模型自发行为**。所有机制必须由 harness 强制触发；模型主动调用修订工具是加分项，
   不是前提。

---

## 本地开发

```powershell
git clone https://github.com/lhy302/regret-gate.git
cd regret-gate/harness

# 只需要标准库 + PyYAML
py -3.12 -m pip install "PyYAML>=6.0"

# 全部测试（268 个用例，离线 FakeClient，不需要任何 API 密钥）
py -3.12 -m unittest discover -s tests -v

# 离线跑通 A–H 并出报告
py -3.12 -m experiments.runner --group all --limit 20
py -3.12 -m experiments.report --metrics runs/metrics.jsonl --out report.md
```

打包 exe（可选）：

```powershell
py -3.12 -m pip install "pyinstaller>=6.0"
py -3.12 -m PyInstaller --noconfirm --clean --distpath dist --workpath build regret_gate_harness.spec
```

---

## 提 PR 之前

- **测试必须全绿**，且新增机制要带对应测试。CI 会在 `ubuntu-latest` 与 `windows-latest`
  上跑 Python 3.10 / 3.12 四组矩阵。
- **不要提交密钥**：任何 `sk-` / `ghp_` / `github_pat_` / `.env` 都不要入库。
  `.gitignore` 已过滤，但请自查 `git diff --cached`。
- **不要提交构建产物**：`dist/`、`build/`、`output/`、`runs/`、`*.jsonl`、`*.log`。
  二进制走 [Releases](https://github.com/lhy302/regret-gate/releases) 附件，不进 git 历史。
- **不要伪造实验结果**：
  - 离线（`FakeClient`）结果必须标明是离线，且不得写成模型能力结论；
  - 样本里某个分支从未被触发时（例如没有发生任何 `reject`），结论必须写「无法测量」，
    **不能**写「不成立」；
  - 失败 run 不得静默丢弃，必须计入分母并出现在报告里。

---

## 想加东西的话

| 想做的事 | 改哪里 | 注意 |
|---|---|---|
| 加一项机制 | `harness/core/` 新模块 → `ExperimentConfig.enabled_mechanisms` 加字段 → 组配置声明 → 在 `experiments/harness.py` 接进回合流程 | 机制必须由 harness 强制触发 |
| 加实验组 | `harness/configs/groups/X_*.yaml` + `experiments/runner.py::GROUP_ORDER` | 组间只允许 `prompt_condition` 与 `enabled_mechanisms` 两个变量 |
| 加工具 | `core/tool_call_parser.py::default_registry()` + `configs/base.yaml` 的 `risk_policy.rules` | 风险元数据由 harness 登记，**不信任模型声明** |
| 加验证器 | `experiments/validators.py` | 返回统一的 `{passed, syntax_error, logic_error, details}` |
| 加任务 | `experiments/generate_tasks.py` 后重新生成 | 必须带 `expected_error_prone_areas` / `dangerous_operations` / `baseline_difficulty` |
| 改 RiskRouter 规则 | `configs/base.yaml` | 规则级误拦截率 > 20% 必须重修（§4.5） |

---

## 文档权威顺序（冲突时以此为准）

1. `docs/后悔承诺门 · Harness 构建规范 v1.0.md` —— 工程实现细则
2. `docs/后悔承诺门 · Harness 验证补充文档 v0.2.md` —— 实验设计与硬约束
3. `docs/后悔承诺门-v4.1.md` —— 设计目标总纲（大量内容已移出范围）

规范未覆盖的工程细节记在 `harness/open_questions.md`，按最保守方案实现，**不得自行扩实验范围**。

---

## 提交信息风格

```
feat: 新增 X 机制（一句话说清作用面）
fix: 修正 Y 在 Z 情况下的错误行为
docs: 更新 W 说明
test: 补充 V 的回归用例
chore: 打包/CI/依赖调整
```

## 许可证

提交即表示同意以 [MIT](./LICENSE) 许可发布你的贡献。
