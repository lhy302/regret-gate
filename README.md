# 后悔承诺门 · 试验台

[![CI](https://github.com/lhy302/regret-gate/actions/workflows/ci.yml/badge.svg)](https://github.com/lhy302/regret-gate/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/lhy302/regret-gate)](https://github.com/lhy302/regret-gate/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Tests](https://img.shields.io/badge/tests-268%20passed-brightgreen)](#验收基线实现后必须全绿)

---

## 🧭 冷启动：先读这个

> **本仓库是给「接手的 Agent」和「照着工程稿做实践的人」准备的。** 别从零摸索，按下面顺序读。

### 你是哪种读者？

| 你的身份 | 从这里开始 | 预计 |
|---|---|---|
| **接手本仓库的 Agent** | **[`接手摘要.md`](./接手摘要.md)** ← **唯一必读**。含当前状态、真实性边界、§0.1 冷启动导航、环境坑清单 | 15 分钟 |
| **想照着工程稿做实践** | **[`docs/协同进化/索引.md`](./docs/协同进化/索引.md)** → [`工程实践项目总表.md`](./docs/协同进化/工程实践项目总表.md) → [`项目规格书 · 当前可做.md`](./docs/协同进化/项目规格书%20·%20当前可做.md) | 30 分钟 |
| **只想跑一下离线实验** | 下面「[下载即用](#下载即用windows无需装-python)」一节 | 5 分钟 |
| **想看研究方向的来龙去脉** | [`docs/协同进化/思考轨迹分析/思考轨迹分析.md`](./docs/协同进化/思考轨迹分析/思考轨迹分析.md) | 20 分钟 |
| **想复现 KV 语义实测** | [`docs/协同进化/experiments/`](./docs/协同进化/experiments/)（4 个脚本，纯 CPU 可跑） | 10 分钟 |
| **不熟悉 git，想知道怎么提交** | [`docs/Git操作手册.md`](./docs/Git操作手册.md)（含英文提示中文对照、凭据弹窗怎么填） | 10 分钟 |

### ⚠️ 两条必须知道的硬约束

1. **本仓库不含模型权重与框架源码。**
   `协同进化-资产/`（Qwen3 权重 1.45 GB、ik_llama.cpp / llama.cpp 源码 276 MB、微调工具链 53 MB）
   **未入库**。要跑实验须按
   [`docs/协同进化/版本与来源清单.md`](./docs/协同进化/版本与来源清单.md) 自行下载
   （内含 HF 镜像 / ModelScope 命令、`git clone` 地址与 commit hash）。

2. **KV 相关工程不许跳过 G2 门。**
   G2 = 测量「一次 KV 编辑对后缀 KV 的实际影响半径」。
   在它被测量之前，后续项目都是在未知地基上盖楼。
   **若测出影响半径 ≈ 整条后缀，该方向应诚实结题** —— 这也是合格产出。

---

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

我的回答分两步走，**不急着造新架构**：

### 第一步（已完成）· 外挂 harness，把缺口逼出来

修订栈让模型登记对本回合输出的修改；尾部强制自审逼它在落地前回头；射程锁保护历史与用户输入；风险分级拦截不可逆操作。整套机制只操作字符串与 messages，**不碰 KV cache、不依赖后训练、不假设厂商协作**。

**目标**：在零后训练、零厂商协作、缓存行为不变的硬约束下，测出「会回头」的模型需要哪些原生能力，以及外挂能把这个缺口补到什么程度。

> 状态：实现与**离线**验证已完成（268 tests / 200 runs，全部基于 `FakeClient`）。
> **真实 API 尚未跑过** —— 这是第一步剩下的唯一有意义的工作。

### 第二步（已规划，未开工）· 协同进化：KV 局部编辑 + 后训练

第一步暴露出的边界是：**有些能力靠外挂补不上**（生成中主动回头检查、错误归因、最小改动修订）。这些需要模型侧配合，也需要把「回头改」落到**缓存层**而不只是字符串层。

于是有了第二步，文档在 **[`docs/协同进化/`](./docs/协同进化/)**：

| 方向 | 一句话 | 状态 |
|---|---|---|
| **KV 局部编辑** | 一轮输出结束后，对 KV cache 做局部手术（替换/删除/插入），避免整段重算 | 设计完成，4 项语义已实测 |
| **推理轨迹纠错** | 推理模型在 `<think>` 里写错了，当前流式输出**没有任何办法回头改** | 已识别为最高价值空白 |
| **后训练** | 让模型自己学会「发现错了并最小改动地改」 | 规划完成，受 4 GB 显存约束 |

**第二步与第一步的关系**：第一步的结论**不改写**第二步的假设；两步必须能**独立开关**，否则无法归因。

> ⚠️ **注意范围变化**：第一步明确把「KV cache 操作」**移出范围**（见下文）。
> 第二步**重新纳入**它 —— 因为在本地推理路径上，KV 是可控的。
> 这个转正是有意的，理由与代价都写在
> [`docs/协同进化/规范缺口与阶段定位.md`](./docs/协同进化/规范缺口与阶段定位.md)。

### 二者的共同点

**每一次崩溃，都是一条下一架构的需求；每一次探索，都是在回答下一个架构可以怎么造。**


---

## 文档权威顺序（冲突时）

### 第一步：harness 实验（`docs/` 根）

1. **[`docs/后悔承诺门 · Harness 构建规范 v1.0.md`](./docs/后悔承诺门%20·%20Harness%20构建规范%20v1.0.md)** — 工程实现细则，**冲突时以它为准**；给编码 Agent 的构建指令，要求「读完可一次性写出可运行 harness」。
2. **[`docs/后悔承诺门 · Harness 验证补充文档 v0.2.md`](./docs/后悔承诺门%20·%20Harness%20验证补充文档%20v0.2.md)** — 实验设计与硬约束（对 v4.1 冲突处以 v0.2 为准）；只测 4 项可验证机制。
3. **[`docs/后悔承诺门-v4.1.md`](./docs/后悔承诺门-v4.1.md)** — 设计目标总纲（KV/重算等大量内容已明确**移出**第一步范围）。

### 第二步：协同进化（`docs/协同进化/`）

**尚无正式规范** —— 这是开工前的第一个待办，模板见
[`规范缺口与阶段定位.md`](./docs/协同进化/规范缺口与阶段定位.md) §4（「一页纸」）。
现有文档是**技术附件**，不自带规范效力。

---

## 第一步的范围界定

### 一句话

在**零后训练、零厂商协作、托管 API 缓存行为不变**的硬约束下，构建实验 harness，测量 4 项外部强制机制及 A–H 组合对任务错误率的真实影响。

### 移出第一步范围（勿在 harness 里做）

KV cache 操作 / 从编辑点重算作为核心贡献 / 下游偏移蒸发 / 生成中主动纠错作为机制核心 / 任何依赖模型自发行为的机制。

> 📌 上列各项**是第二步的对象**，不是永久禁区。改动前请先读
> [`docs/协同进化/规范缺口与阶段定位.md`](./docs/协同进化/规范缺口与阶段定位.md)。

### 第一步的 4 项可验证机制

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
├── README.md          ← 本文件
├── 接手摘要.md        ← ★ 冷启动唯一必读（含 §0.1 导航、真实性边界、环境坑）
├── AGENTS.md          ← Agent 工作入口与文档地图
├── .gitignore
├── docs\
│   ├── 后悔承诺门 · Harness 构建规范 v1.0.md      ← 第一步最高权威
│   ├── 后悔承诺门 · Harness 验证补充文档 v0.2.md
│   ├── 后悔承诺门-v4.1.md
│   └── 协同进化\                              ← ★ 第二步：工程文档与实测脚本
│       ├── 索引.md                            ← 总入口
│       ├── 工程实践项目总表.md                  ← 路线图 + G2 决策门
│       ├── 项目规格书 · 当前可做.md             ← P1–P3（可立即开工）
│       ├── 工程项目清单.md                     ← P4–P10
│       ├── KV缓存手术 · 工程设计与文献基础 v1.0.md
│       ├── Leyline与EVOKE精读笔记.md
│       ├── KV局部编辑 · 研究方向备忘.md
│       ├── 规范缺口与阶段定位.md / 微调环境搭建.md / 版本与来源清单.md
│       ├── setup_微调环境.ps1
│       ├── 思考轨迹分析\  docs_收集\  experiments\
└── harness\           ← 第一步实现（已完成）
    ├── configs\       base.yaml + groups/A–H + tasks/（80 个任务）
    ├── core\          types / payload_builder / stream_collector / tool_call_parser /
    │                  risk_router / pending_actions / revision_stack / tail_audit /
    │                  external_auditor / executor / audit_logger
    ├── llm\           base_client / openai_client / anthropic_client / fake_client / generators
    ├── experiments\   harness（编排）/ runner / metrics / stats / analysis / report /
    │                  validators / generate_tasks / summarize
    ├── tasks\         schema / loader
    ├── tests\         13 个测试模块，268 个用例全绿
    ├── report.md      离线 A–H 实验结果报告（200 次 run）
    └── open_questions.md  规范未覆盖细节与所选最保守方案
```

> **未在仓库中的目录**：`协同进化-资产\`（模型权重 / 框架源码 / 微调工具链，约 1.78 GB）
> —— 按 `docs/协同进化/版本与来源清单.md` 自行下载。

**验收状态**：`py -3.12 -m unittest discover -s tests -v` → **268 passed**；
`py -3.12 -m experiments.runner --group all --limit 20` → **200 runs / 0 失败**；
`report.md` 已生成（离线 FakeClient，仅验证链路与指标口径，不构成模型能力结论）。
CI 矩阵（ubuntu + windows × py3.10/3.12）在 `main` 上 4/4 全绿。

**仓库状态**：`origin` 为 `lhy302/regret-gate`，远程 `main` 与本地 HEAD 一致。
历史遗留的 **force 覆盖**流程已作废——当前是常规 `commit → push`，不要再 force。
外部 PR #2 / #3 已合并关闭；本地运维手册（推送指南）**不入库**，由 `.gitignore` 忽略。

## 参与

- 贡献前请读 [`CONTRIBUTING.md`](./CONTRIBUTING.md)；变更记录见 [`CHANGELOG.md`](./CHANGELOG.md)。
- **第二步（协同进化）尚缺正式规范** —— 若你想参与，那是投入产出比最高的切入点。
