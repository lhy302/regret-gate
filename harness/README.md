# 后悔承诺门 · Harness（实验 harness 参考实现）

> 本目录是按《后悔承诺门 · Harness 构建规范 v1.0》与《Harness 验证补充文档 v0.2》
> 从零实现的实验 harness。它只做一件事：
> **在零后训练、零厂商协作、托管 API 缓存行为不变的硬约束下，测量 harness 侧四项外部强制机制的真实潜力。**

---

## 0. 快速开始

```powershell
cd harness

# 1) 全部单元测试 + 端到端验收（离线 FakeClient，不需要任何凭据）
py -3.12 -m unittest discover -s tests -v

# 2) 离线跑完 A–H 全部实验组（每组 20 个任务 → 200 次 run）
py -3.12 -m experiments.runner --group all --out runs --limit 20

# 3) 生成 Markdown 报告
py -3.12 -m experiments.report --metrics runs/metrics.jsonl --out report.md --meta runs/meta.json

# 4) 可选：终端速览 + 结构化分析（JSON）
py -3.12 -m experiments.summarize --metrics runs/metrics.jsonl --json runs/analysis.json
```

> `--limit` 是**按类别轮转**取样，因此 `--limit 20` 得到“每类 5 个”，不会只取到
> 第一个任务文件（直接切片会让分层分析与“每类都有样本”的验收失效）。

产物：

```
runs/metrics.jsonl              # 每次 run 一条指标（§13.2）
runs/metrics/<组>.jsonl         # 分组指标（逐组运行时可累积）
runs/audit/<run_id>.jsonl       # 每个 run 的不可变审计时间线（§10）
runs/runs/<run_id>.json        # 每个 run 的结构化结果
runs/summary.json               # 运行汇总
report.md                       # 报告（§13.4）
open_questions.md               # 规范未覆盖细节与所选最保守方案（§17）
```

---

## 1. 它在测什么（本实验的可验证边界）

只有**由 harness 强制触发、不依赖模型自发行为**的四项机制进入实验范围（v0.2 §0.2）：

| # | 机制 | 模块 | 规范锚点 |
|---|---|---|---|
| 1 | 参数级工具调用风险分级 | `core/risk_router.py` | §4 |
| 2 | 尾部强制自审（每回合必注入） | `core/tail_audit.py` | §7 |
| 3 | 独立审核 Agent + 检查清单 | `core/external_auditor.py` | §8 |
| 4 | 带射程锁的上下文编辑协议 | `core/revision_stack.py` | §6 |

**明确移出范围**（v0.2 §0.1，本 harness 一行都不碰）：KV cache 操作、从编辑点重算作为贡献、
下游偏移蒸发、前缀缓存失效、任何依赖模型自发行为的机制、后训练/微调/未公开接口。
harness 只记录 `cache_break_point`（payload 最早变化位置）作为**成本观测量**，不当机制贡献。

## 2. 目录与模块边界

```
harness/
├── configs/          base.yaml + groups/A–H + tasks/{long_code,long_text,short_command,mixed}
├── core/             payload_builder, stream_collector, tool_call_parser, risk_router,
│                     pending_actions, revision_stack, tail_audit, external_auditor,
│                     executor, audit_logger, types
├── llm/              base_client, openai_client, anthropic_client, fake_client, generators
├── experiments/      harness(编排), runner, metrics, stats, analysis, report, validators,
│                     generate_tasks
├── tasks/            schema, loader
├── tests/            模块单测 + 端到端四场景 + 确定性与配置矩阵
└── README.md / open_questions.md
```

**边界铁律（§1）**

- `core/` **不 import `llm/`**：所有 LLM 调用通过调用方注入的 `LLMClient` 抽象
  （鸭子类型：`stream_chat` / `complete`）。
- `experiments/` **不直连任何 provider SDK**，只调用 `core/` 与 `llm/` 的公共接口。
- `core/` 内部只通过 `core/types.py` 的数据结构通信，**无全局状态**。
- 所有配置从 `configs/` 读取，代码里不硬编码模型、key、路径。

## 3. 数据流（一个回合）

```
① PayloadBuilder.build() 构造 messages（system + 历史 + 本回合输入）
② LLMClient.stream_chat() 流式返回
③ StreamCollector 逐 chunk：
     text        → base_text（只进审计，绝不进下一次 messages）
     tool_call   → ToolCallParser 校验 → RiskRouter 分级
                   ├ allow    → 立即执行
                   ├ buffer   → PendingActions（尾部自审通过后执行）
                   └ intercept→ PendingActions（尾部自审 + 独立审核通过后执行）
     draft.commit_revision → RevisionStack（登记，不改变外部状态）
④ 尾部强制自审：TailAudit 注入一次 role=user 提示（每回合必注入，一次）
⑤ RevisionStack.finalize() → finalized_text（按 created_at 升序；失败即 rejected）
⑥ PendingActions 决议：buffer 走自审；intercept 必过 ExternalAuditor（只传操作 + 清单）
⑦ PayloadBuilder 用 finalized_text 构造下一次 payload（base_text 不出现）
⑧ 审计日志记完整时间线；用户可见的是 finalized_text
```

## 4. 配置

### 4.1 实验组（唯一变量：`prompt_condition` + `enabled_mechanisms`）

| 组 | prompt | risk_router | tail_audit | external_auditor | revision_stack |
|---|---|---|---|---|---|
| A | none | ✗ | ✗ | ✗ | ✗ |
| B | none | ✓ | ✗ | ✗ | ✗ |
| C | none | ✗ | ✓ | ✗ | ✗ |
| D | none | ✗ | ✗ | ✓ | ✗ |
| E | none | ✓ | ✓ | ✗ | ✗ |
| F | none | ✓ | ✓ | ✓ | ✓ |
| G | strong | ✓ | ✓ | ✓ | ✓ |
| H | none/weak/strong | ✗ | ✗ | ✗ | ✓ |

`H` 由 runner 自动展开为 `H-none` / `H-weak` / `H-strong`，共享同一任务集、同一采样参数。
`configs/base.yaml` 里除这两项之外的一切（模型、采样、超时、任务集）在所有组之间完全一致，
`tests/test_end_to_end.py::ConfigMatrixTest` 会强制校验这一点。

风险策略由 `configs/base.yaml` 的 `risk_policy` 经运行器传入 harness，各组共享同一策略；
规则、兜底级别、大小写、复合命令开关与路径配置均使用该配置。
直接构造 `ExperimentConfig` 时，`risk_policy=None` 保留历史默认策略；显式传入 `{}`
则表示无匹配规则，使用 RiskPolicy 的保守兜底（`intercept`），不会恢复默认放行规则。

### 4.2 命令行

```powershell
py -3.12 -m experiments.runner --group F --limit 20         # 只跑 F 组
py -3.12 -m experiments.runner --group A,B,F --limit 5      # 跑指定组
py -3.12 -m experiments.runner --group H --limit 20         # 自动展开 H 三个子组
py -3.12 -m experiments.runner --group all --limit 80 --runs 3   # 每组 80 任务 × 3 次
```

### 4.3 切到真实 API

1. 设凭据（**不要写进配置或代码**）：

```powershell
$env:OPENAI_API_KEY = "..."          # provider: openai
$env:ANTHROPIC_API_KEY = "..."       # provider: anthropic
```

2. 在 `configs/base.yaml` 里改：

```yaml
provider: openai
sampling:
  model: gpt-4o-2024-08-06   # 必须写死具体版本，禁止滚动别名（§11.5 / 铁律 10）
  temperature: 0.0
  top_p: 1.0
  seed: 0
```

3. 执行器默认 `dry_run`，不会真正落地写操作或执行外部命令；
   若要真实执行，请自行实现 `core/executor.py` 中的 `LocalExecutor` 并在 runner 里注入 ——
   **本实验默认不提供真实 shell 执行**（避免实验污染主机）。

## 5. 任务集

`configs/tasks/*.yaml`，每类 20 个共 80 个，由 `experiments/generate_tasks.py` 生成：

| 类别 | 规模 | 验证器 | 标注 |
|---|---|---|---|
| `long_code` | 20 个，要求 > 300 行 | `compile`（`ast.parse`） | 易错位置 / 难度 |
| `long_text` | 20 个，要求 > 2000 字 | `regex` | 易错位置 / 难度 |
| `short_command` | 20 个，含高危命令 | `regex` | `dangerous_operations` |
| `mixed` | 20 个，先写代码再执行命令 | `compile` | 易错位置 / 难度 |

重新生成（改任务集后）：

```powershell
py -3.12 -m experiments.generate_tasks
```

## 6. 指标

每条 `metrics.jsonl` 记录（§13.1/§13.2）：
`run_id / group / task_id / category / syntax_error / logic_error / security_incident /
false_intercept / input_tokens / output_tokens / latency_ms / tail_audit_modified /
external_audit_effective / revision_hit_rate / task_success / cache_break_ratio / error`，
另有 `intercepts / matched_rule / llm_calls / audit_log_bytes / tail_audit_* / external_audit_*` 等观测量。

报告同时给出**均值、中位数、标准差、95% CI**（只报均值是明确的错误，§15.3）、
配对检验（t 值、p 值、Cohen's d_z 与池化 d、差值 95% CI）、按类别分层、失败案例清单、
缓存破坏点分布，以及 H1–H6 的判定与 v0.2 §8 的结果解读。

## 7. 如何扩展

- **加机制**：在 `core/` 新增模块 → `ExperimentConfig.enabled_mechanisms` 加字段 →
  `configs/groups/*.yaml` 声明 → `experiments/harness.py` 里接进回合流程。
  只要遵守“机制必须由 harness 强制触发”这条，就不会污染实验设计。
- **加实验组**：`configs/groups/` 放 `X_*.yaml`，`experiments/runner.py::GROUP_ORDER` 加 `X`。
- **加工具**：`core/tool_call_parser.py::default_registry()` 注册 `ToolSpec`
  （含 `effect_type` 与默认级别），再在 `configs/base.yaml` 的 `risk_policy.rules` 加规则。
  注意：风险元数据由 harness 侧登记，**不信任模型声明**（§4.4）。
- **加验证器**：`experiments/validators.py` 加分支，返回统一的
  `{passed, syntax_error, logic_error, details}` 结构。
- **换冻结的离线行为**：`llm/fake_client.py` + `llm/generators.py`
  （`FakeClient(script=[...])` 可完全脚本化，用于定向单测）。

## 8. 验收对照

| 规范条目 | 落实位置 |
|---|---|
| §14.1 七个模块单测（FakeClient，不调真实 API） | `tests/test_{tool_call_parser,risk_router,revision_stack,tail_audit,external_auditor,payload_builder,audit_logger}.py` |
| §14.2 场景 1 短命令拦截链路 | `tests/test_end_to_end.py::Scenario1ShortCommandTest` |
| §14.2 场景 2 长代码 finalize | `Scenario2LongCodeTest` |
| §14.2 场景 3 H 矩阵 | `Scenario3HMatrixTest` |
| §14.2 场景 4 离线完整实验 + 报告 | `Scenario4OfflineExperimentTest` |
| §14.3 性能（≤10 次 API 调用 / 审计 < 5 MB / 延迟 < timeout） | `Scenario4OfflineExperimentTest`、报告 §10 |
| §14.4 可复现（连跑两次指标一致） | `DeterminismTest` |
| 机制不空转（D 组必须真的调用审核 Agent） | `tests/test_mechanism_activation.py` |
| 指标口径（布尔不得恒为 1、必须报分布） | `tests/test_metrics_analysis.py` |
| 组矩阵与“唯一变量”约束 | `tests/test_runner_config.py`、`ConfigMatrixTest` |
| §16 交付清单 | 目录结构 + 80 任务 + 全部 configs + `report.md` + 本文件 + `open_questions.md` |

当前规模：**268 个测试全绿**，离线 A–H 共 **200 次 run、0 失败**。

## 9. 离线报告的定位（重要）

`report.md` 由 **FakeClient** 跑出，**唯一用途是验证 harness 链路与指标口径**：
它证明拦截—暂存—自审—finalize—审核—执行这条链能跑通、审计能还原、指标能算、报告能出。
它**不是**模型能力结论，也**不是**后训练价值结论。任何对外结论都必须在真实 API 上重跑，
并固定模型版本、采样参数与种子。

两条必须同时读的口径说明：

- 离线运行注入**固定步长时钟**（`DeterministicClock`，每步 500µs），所以报告里的
  `latency_ms` 只反映“请求次数 × 固定步长”，**不代表真实性能**；
- 若某一假设的分支在样本里从未被触发（例如离线审核桩恒返回 `approve`，从未 `reject`），
  报告会标为**“无法测量”而不是“不成立”**（见 H5）。把“没测到”写成“没效果”是伪造结论。
