# 后悔承诺门 · 试验台

[![CI](https://github.com/lhy302/regret-gate/actions/workflows/ci.yml/badge.svg)](https://github.com/lhy302/regret-gate/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/lhy302/regret-gate)](https://github.com/lhy302/regret-gate/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Tests](https://img.shields.io/badge/tests-262%20passed-brightgreen)](#验收状态)

## 下载即用（Windows，无需装 Python）

从 **[Releases](https://github.com/lhy302/regret-gate/releases/latest)** 下载
`regret-gate-harness-*-windows-x64.zip`，解压后双击 `regret-gate-harness.exe`：

- 打开**图形启动器**，填 **API 地址 / API 密钥 / 模型名称**（模型名可点「获取模型列表」自动拉取）；
- 或直接点「开始实验」用内置离线桩跑通 A–H 全组，不需要任何密钥；
- 产出在 exe 同级的 `output/`（`report.md` 是主结果）。

命令行同样可用：

```powershell
.\regret-gate-harness.exe selftest                      # 离线自检
.\regret-gate-harness.exe run --provider fake --limit 20 # 离线跑 A–H
$env:REGRET_GATE_API_KEY = "sk-..."                      # 真实 API 走环境变量，不进命令行
.\regret-gate-harness.exe run --provider openai --model gpt-4o-2024-08-06 --group A,F --limit 4
```

> ⚠️ **先读真实性声明**：仓库自带的 `report.md` 与 CI 报告**全部来自离线 `FakeClient`**，
> 只验证机制链路与指标口径；**真实 API 未跑**，因此其中的错误率/成功率**不是模型能力结论**。
> 假设 **H5 在离线数据里是「无法测量」而不是「不成立」**（审核桩恒返回 approve）。
> 发行版执行器为 **dry-run**：高危命令只记录、不执行，不会改动你的机器。

从源码构建见 [`harness/README.md`](./harness/README.md)；
重新打包 exe：`cd harness && py -3.12 -m PyInstaller --noconfirm --clean regret_gate_harness.spec`。

---

## 这个项目是做什么的

这是一个**探针项目**，不是产品。它想回答一个问题：**Transformer 究竟缺了什么？**

我的判断是：它缺一条「写通道」。

人类的注意力是读写双向的。听到「不要做 X」，大脑不是把「不要做 X」这几个字存下来，而是直接改写 X 的内部状态——激活阈值调高，关联路径抑制，替代路径激活。否定变成**状态**，不是字符串。AI 的注意力是**只读的**。听到「不要做 X」，它只能把这条信息读进上下文，权重纹丝不动。否定变成**字符串**，不是状态。所以它会反复复述「得用这个概念的反概念」，而不是真的把否定内化为状态。

上下文再长，也只是外部记忆。没有通往内部权重的路。窗口内的 token 再多，模型本身还是那个模型。

我的回答：**不急着造新架构，先用 harness 外挂把这个缺口逼出来。**

修订栈让模型登记对本回合输出的修改；尾部强制自审逼它在落地前回头；射程锁保护历史与用户输入；风险分级拦截不可逆操作。整套机制只操作字符串与 messages，不碰 KV cache，不依赖后训练，不假设厂商协作。

**实验目标只有一个**：在零后训练、零厂商协作、缓存行为不变的硬约束下，测出「会回头」的模型需要哪些原生能力，以及外挂能把这个缺口补到什么程度。

每一次崩溃，都是一条下一架构的需求；每一次探索，都是在回答下一个架构可以怎么造。


---

## 文档权威顺序（冲突时）

1. **[`docs/后悔承诺门 · Harness 构建规范 v1.0.md`](./docs/后悔承诺门%20·%20Harness%20构建规范%20v1.0.md)** — 工程实现细则，**冲突时以它为准**；给编码 Agent 的构建指令，要求「读完可一次性写出可运行 harness」。
2. **[`docs/后悔承诺门 · Harness 验证补充文档 v0.2.md`](./docs/后悔承诺门%20·%20Harness%20验证补充文档%20v0.2.md)** — 实验设计与硬约束（对 v4.1 冲突处以 v0.2 为准）；只测 4 项可验证机制。
3. **[`docs/后悔承诺门-v4.1.md`](./docs/后悔承诺门-v4.1.md)** — 设计目标总纲（KV/重算等大量内容已明确**移出**本实验范围）。

---

## 一句话

在**零后训练、零厂商协作、托管 API 缓存行为不变**的硬约束下，构建实验 harness，测量 4 项外部强制机制及 A–H 组合对任务错误率的真实影响。

## 移出范围（勿做）

KV cache 操作 / 从编辑点重算作为核心贡献 / 下游偏移蒸发 / 生成中主动纠错作为机制核心 / 任何依赖模型自发行为的机制。

## 本实验 4 项可验证机制

1. 参数级工具风险分级（RiskRouter）
2. 尾部强制自审（每回合必注入一次）
3. 独立审核 Agent + 检查清单
4. 带射程锁的上下文编辑（RevisionStack + `draft.commit_revision`）

---

## 构建入口

- **构建指令**：`docs/…构建规范 v1.0.md` 第 1 节目录结构、第 14 节验收、第 15 节错误清单、第 16 节交付清单。
- **实现规范覆盖**：§2 LLM 抽象、§3 工具注册、§4 RiskRouter、§5 PendingActions、§6 RevisionStack、§7 TailAudit、§8 ExternalAuditor、§9 PayloadBuilder、§10 AuditLogger、§11 Runner、§12 任务集、§13 指标。
- **离线优先**：`llm/fake_client.py` + FakeClient 跑 A–H 全组验收（§14.2 场景 1–4），不依赖真实 API。
- **环境**：`py -3.12`（已确认 3.12.10 + PyYAML 6.0.2 可用）。

## 验收基线（实现后必须全绿）

```powershell
# 在 harness/ 目录
py -3.12 -m unittest discover -s tests -v
```

交付清单见构建规范 §16（含 80 任务、A–H 配置、离线 report.md）。

---

## 目录现状

```
工作区4\
├── README.md          ← 本文件（开头为作者：项目是做什么的）
├── 接手摘要.md        ← 冷启动唯一必读（含当前状态头与已完成 checklist）
├── .gitignore          ← 已过滤 token/env/缓存（单独放行 harness/report.md）
├── docs\
│   ├── 后悔承诺门-v4.1.md
│   ├── 后悔承诺门 · Harness 构建规范 v1.0.md      ← 构建最高权威
│   └── 后悔承诺门 · Harness 验证补充文档 v0.2.md
└── harness\           ← 已按规范构建完成
    ├── configs\       base.yaml + groups/A–H + tasks/（80 个任务）
    ├── core\          types / payload_builder / stream_collector / tool_call_parser /
    │                  risk_router / pending_actions / revision_stack / tail_audit /
    │                  external_auditor / executor / audit_logger
    ├── llm\           base_client / openai_client / anthropic_client / fake_client / generators
    ├── experiments\   harness（编排）/ runner / metrics / stats / analysis / report /
    │                  validators / generate_tasks / summarize
    ├── tasks\         schema / loader
    ├── tests\         11 个测试模块，227 个用例全绿
    ├── report.md      离线 A–H 实验结果报告（200 次 run）
    └── open_questions.md  规范未覆盖细节与所选最保守方案
```

**验收状态**：`py -3.12 -m unittest discover -s tests -v` → **227 passed**；
`py -3.12 -m experiments.runner --group all --limit 20` → **200 runs / 0 失败**；
`report.md` 已生成（离线 FakeClient，仅验证链路与指标口径，不构成模型能力结论）。

**GitHub 预置状态**：本地仓已 `init`（分支 `main`）、身份与纯净 `origin`（`lhy302/regret-gate`）已配、`.gitignore` 已建、文档已本地提交。接班 Agent 完成构建后按推送指南 **force 覆盖**远程旧内容即可。
