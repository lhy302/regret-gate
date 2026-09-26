# 变更记录

本文件按「用户可见的变化」记录，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- `AGENTS.md`：Agent 工作入口与文档导航（入口、用途、阅读时机、规范优先级），不复制规范正文。

### Fixed

- `configs/base.yaml` 的 `risk_policy` 现已真正接入实验主流程。此前 `runner` 不读取该键、
  `ExperimentConfig` 也没有对应字段，`MechanismHarness` 始终使用内置硬编码规则表，
  导致 YAML 中的风险策略是「死配置」，改动会静默失效（外部贡献 PR #3）。
  经复核，该缺陷**未影响已发布的 v1.0.0 离线结果**：修复前后 A–G 逐任务
  `intercept_count` / `executed_actions` / `false_intercept` 完全一致。

### Changed

- 文档数字与状态核对到当前实现：测试规模 227/262 → **268**（`README.md` 徽章与验收状态、
  `CONTRIBUTING.md`、`harness/README.md`、`接手摘要.md`）；`README.md` 中「完成构建后
  force 覆盖远程」的预置说明已作废，改为常规 `commit → push`。

## [1.0.0] - 2026-09-26

首个可交付版本：按《后悔承诺门 · Harness 构建规范 v1.0》从零构建的实验 harness，
并打包为 Windows 单文件 exe。

### Added

**四项机制实现（`harness/core/`）**

- `risk_router`：参数级工具调用风险分级。规则表 + `default=intercept` 兜底；
  命令拼接（`&&` / `;` / `|`）拆分取最严；路径遍历与越权路径拦截；每条决策带 `matched_rule` 标签。
- `tail_audit`：尾部强制自审。**每回合必注入一次**（不是拦截后才注入），以 `role: user` 注入，
  超时 30s 记录 `tail_audit_timeout`，注入后额外生成循环上限 3 次。
- `external_auditor`：独立审核 Agent + 8 项检查清单。**只传操作与清单，绝不传对话历史**；
  JSON 解析失败 → `needs_revision`；输入 ≤4000 字符、输出 ≤500 token、超时 20s、每回合次数 ≤ 待决数。
- `revision_stack`：带射程锁的上下文编辑。`resolve_range` 只做三级匹配（精确 → 去空白 → 空白归一化），
  **禁止模糊/语义匹配**；按 `created_at` 升序应用；射程锁硬编码在栈层（只持有本回合 `base_text`）。

**配套模块**

- `payload_builder`：四条铁律（`base_text` / 修订史 / 自审提示 / pending 内部态都不进 messages）+ `cache_break_point`。
- `stream_collector`：累积到 `tool_call_end` 才解析 JSON；无效 JSON 标 `malformed`；流中断标 `incomplete`；ID 去重。
- `pending_actions`：`pending → approved/rejected/revised → executed → audited` 状态机。
- `audit_logger`：JSONL 追加不可变；16 种事件类型；`ts/turn/event/run_id/group/task_id/data` 固定字段。
- `executor`：`DryRunExecutor`（默认，只记录）与受限 `LocalExecutor`。
- `llm/`：`base_client` 抽象 + `openai_client` / `anthropic_client`（含 SSE 解析、`request_id` 捕获、
  重试与指数退避）+ 确定性 `fake_client` + `model_list` 模型列表探测。
- `experiments/`：`harness`（回合编排）、`runner`、`metrics`、`stats`（自实现 t 分布）、`analysis`、
  `report`、`validators`、`generate_tasks`、`summarize`、`paths`、`job_runner`、`gui`。

**实验与任务集**

- A–H 八个实验组配置，`H` 自动展开为 `H-none` / `H-weak` / `H-strong`；组间唯一变量为
  `prompt_condition` 与 `enabled_mechanisms`（由测试强制校验）。
- 80 个任务（`long_code` / `long_text` / `short_command` / `mixed` 各 20），带
  `expected_error_prone_areas` / `dangerous_operations` / `baseline_difficulty` 标注。
- 报告包含：分组均值+中位数+标准差+95% CI、配对 t 检验（Cohen's d_z 与池化 d）、
  分层分析、失败案例、缓存破坏点分布、H1–H6 判定、性能预算验收。

**可执行版与启动器**

- 单文件 exe（PyInstaller，`--windowed`），不带参数即打开图形启动器。
- 启动器三字段：**API 地址 / API 密钥 / 模型名称**；模型名可点「获取模型列表」从 `/models` 拉取。
- 密钥只经环境变量传给子进程，**不进命令行、不进日志、不进审计**；默认不落盘。
- 子进程以 `CREATE_NO_WINDOW` 启动，输出重定向到日志文件（windowed exe 无 stdout）。
- CLI 子命令：`run` / `report` / `init` / `selftest` / `gui`。

**测试**

- 262 个用例：7 个 core 模块单测、端到端 4 场景、确定性（连跑两次指标一致）、
  配置矩阵与「唯一变量」校验、机制不空转（D 组必须真的调用审核 Agent）、
  指标口径（布尔不得恒为 1）、启动器与 GUI 冒烟。

### Scope

明确**不在**本实验范围（v0.2 §0.1）：KV cache 操作、从编辑点重算作为贡献、下游偏移蒸发、
前缀缓存失效、任何依赖模型自发行为的机制、后训练与微调。harness 只记录 `cache_break_point`
作为成本观测，不作为机制贡献。

### Known limitations

- 发行版执行器为 **dry-run**，高危命令只记录不执行（安全默认，需自行打开才落地）。
- 任务集未附带 `unit_test` 测试文件，验证器为 `compile`（代码类）与 `regex`（文本/命令类）。
- 离线报告**不能**作为模型能力或后训练价值结论；真实结论必须在真实 API 上重跑。
- 假设 **H5 在离线数据里「无法测量」**（审核桩恒 `approve`，`reject` 分支未触发），不是「不成立」。

[Unreleased]: https://github.com/lhy302/regret-gate/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/lhy302/regret-gate/releases/tag/v1.0.0
