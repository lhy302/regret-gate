# 协同进化阶段 · 本地资产索引

> 建立时间：2026/09/26
> 对应规范：`docs/后悔承诺门 · Harness 验证补充文档 v0.2.md` **§11 后训练与协同进化方向**
> 本目录**不入库**（已在 `.gitignore` 中忽略），只作为本地工作副本。

---

## 0. 重要前提：这一阶段需要一份新规范

当前规范对"KV 操作 + 后训练"是**明确移出范围**的，动手前必须知道边界在哪：

| 出处 | 原文 | 含义 |
|---|---|---|
| 构建规范 v1.0 §0 铁律 3 | "**不做 KV cache 操作**。不碰 logits、不碰 hidden states、不碰 attention。只操作 messages 数组和字符串。" | 当前实验的硬约束 |
| v0.2 §0.1 | "KV cache 直接操作 → 移出范围（托管 API 不暴露 KV cache，本地路径无法泛化）" | 移出原因 |
| v0.2 §9.3 | "**推迟**：KV cache 局部复用、erratum 追加、近似重算；托管 API 前缀缓存 provider 侧优化；**后训练与微调**" | 标记为推迟，不是否决 |
| v0.2 §11 | "**假设性提纲，当前无法验证**……不是路线图，不是承诺，不是设计目标" | 本阶段的理论出处 |

**结论**：你要做的是把 §9.3「推迟」和 §11「假设」的东西**转正**。这在流程上完全正当，但按本项目自己的规矩（§17：不得自行扩实验范围），**需要一份新的规范/任务书**来定义：

- 本阶段的名字与目标（建议：`协同进化 v1.0` 或 `KV 机制验证 v1.0`）
- 与既有 A–H 实验的关系（并行？取代？前置？）
- 成败判据（否则又是一次"没测到被写成没效果"）
- 与铁律 1/2/3 的冲突如何正式豁免或修订

参见本目录 `规范缺口与阶段定位.md`。

---

## 1. 目录结构

```
协同进化/
├── models/                     模型权重（原始，未经任何修改）
│   └── Qwen3-0.6B/             已核验，见 §3
├── sources/                    推理框架源码（要魔改的对象）
│   ├── ik_llama.cpp/           ★ 主改造目标（KV 操作）
│   └── llama.cpp/              上游参考（对照上游 API 演进）
├── finetune/                   微调工具链
│   ├── Qwen3/                  ★ 官方仓库：训练文档 + 示例配置
│   ├── ms-swift/               阿里官方微调框架（Qwen 一等公民）
│   └── (LLaMA-Factory/ 待装)    备选框架，见 §5
├── docs_收集/                  上游文档摘录、参考链接
├── README.md                   本文件
├── 版本与来源清单.md             commit hash / 文件 hash / 来源 URL
├── 规范缺口与阶段定位.md         与现有规范的冲突点与建议
└── 微调环境搭建.md              环境、依赖、4B 对照模型获取
```

---

## 2. 推理框架源码（KV 魔改的目标）

### 2.1 ik_llama.cpp ★ 主目标

```
sources/ik_llama.cpp/          HEAD 85a3f2c   (浅克隆)
```

**为什么选它做 KV 改造**：它已经在生产环境跑 KV 量化（你现有启动脚本里的 `-ctk q4_0 -ctv q4_0`），说明 KV 这条路径在这个 fork 里是**一等公民**、可配置、可观测，相比上游更适合改。

**它比上游更适合你的一点**：上游 llama.cpp 已把 KV API 重构成 `llama_memory_*`；而 ik_llama.cpp 这里**仍保有 `llama_kv_cache_*` 这一族显式 API**：

```
include/llama.h
  llama_kv_cache_view_init / _free / _update     ← 可枚举 KV 单元状态
  llama_get_kv_cache_token_count                 ← 已用 token 数
  llama_get_kv_cache_used_cells                  ← 已用 cell 数
  llama_kv_cache_is_compacted                    ← 压缩状态
  llama_kv_cache_swa_rewind_floor / _n_swa       ← SWA 相关
```

**要做 KV 层操作，重点看这两处**：

| 文件 | 作用 |
|---|---|
| `include/llama.h` | 公开 C API。**KV 操作的新方法在这里声明** |
| `src/llama-context.h` (29.7 KB) | `llama_context` 内部结构，KV cache 实例挂在这里 |
| `src/llama-hparams.{h,cpp}` | 超参（含 SWA / 量化），KV 行为受此影响 |

并且要 grep 现有实现，判断哪些能力已经有了、不用重写：

```bash
grep -n "llama_kv_cache_seq_rm\|llama_kv_cache_seq_add\|llama_kv_cache_seq_cp" include/llama.h
```

（`seq_rm` / `seq_add` / `seq_cp` 是 llama.cpp 既有的序列级 KV 编辑原语 —— **"删除 KV 区间"和"KV 位置平移"很可能已经有底层实现**，你要做的是把它**暴露成协议方法**，而不是从零实现。这能省掉大量工作。）

### 2.2 llama.cpp（上游参考）

```
sources/llama.cpp/             HEAD 81bc6b8   (浅克隆)
```

用途：对照上游 API 怎么演进（`llama_memory_*`）、以及你魔改后如何保持可合并性。

---

## 3. 模型权重（原始，保留给后训练）

### 3.1 Qwen3-0.6B —— 已核验为官方原版

```
models/Qwen3-0.6B/
  model.safetensors    1,433.66 MB   ← 原始 bf16 权重，未做任何修改
  config.json  tokenizer.json  vocab.json  merges.txt
  tokenizer_config.json  generation_config.json  LICENSE  README.md  .gitattributes
```

**来源核验（三重一致，不是"看起来像"）**：

| 核验项 | 值 |
|---|---|
| HF 仓库 `Qwen/Qwen3-0.6B` commit | `c1899de289a04d12100db370d81485cdf75e47ca` |
| 你原始 zip 内 HF 缓存记录的 commit | `c1899de289a04d12100db370d81485cdf75e47ca` ← **一致** |
| `model.safetensors` SHA-256 | `f47f71177f32bcd101b7573ec9171e6a57f4f4d31148d38e382306f42996874b` |
| zip 内 HF 缓存记录的 SHA-256 | `f47f71177f32bcd101b7573ec9171e6a57f4f4d31148d38e382306f42996874b` ← **一致** |
| 官方文件清单 | 10 个文件，**逐个对上**（无缺无多） |

`config.json` 关键参数：`Qwen3ForCausalLM`、`bfloat16`、28 层、hidden 1024、16 heads / 8 KV heads、vocab 151936、context 40960。

**这份权重是干净的，可以直接作为微调起点。** 桌面的 `Qwen3-0.6B-BF16.gguf`（1.44 GB）也保留着，那是**推理用**的量化容器，**不能用来微调**，两者别混。

### 3.2 Qwen3-4B —— 同族对照实验需要，尚未下载

见 `微调环境搭建.md` §4（含 hf-mirror 下载命令）。

---

## 4. OpenAI 协议魔改（增加 KV 层方法）

目标：在现有 OpenAI 兼容协议上**新增 KV 层操作方法**。改造点在 `ik_llama.cpp` 的 server：

| 文件 | 大小 | 角色 |
|---|---|---|
| `examples/server/server-context.cpp` | 216.8 KB | ★ **核心**：请求处理、slot 与上下文管理，KV 操作最可能落在这里 |
| `examples/server/server.cpp` | 95.9 KB | HTTP 路由与端点注册 |
| `examples/server/server-task.cpp` | 42.5 KB | 任务/请求结构定义 |
| `examples/server/server-common.h` | 18.6 KB | 公共结构体，**新增请求/响应字段在这里** |
| `examples/server/server-chat.cpp` | 22.5 KB | chat completions 解析 |
| `examples/server/README.md` | 52.3 KB | 官方协议文档，**加新方法后要同步更新** |
| `examples/server/function_calls.md` | 10.7 KB | 工具调用协议写法参考 |

**建议的接入层次**（从省事到彻底）：

1. **最省事**：复用既有 `/slots`、`/erase`、`/infill` 之类的旁路端点思路，新增 `/kv/*` 端点族 —— 不动 chat completions 协议本身，风险最小。
2. **中等**：在 `chat/completions` 的请求体里加扩展字段（如 `kv_ops: [...]`），在 `server-context.cpp` 处理请求时应用。
3. **最彻底**：把 KV 操作做成"模型可调用的工具"（tool call），即模型在生成中可通过工具协议请求 KV 编辑 —— **这一条才真正对应 v0.2 §11 的"模型与 harness 协同"**，但需要模型侧后训练配合（§11.1 能力 A–E）。

建议从 1 起步、把 3 作为终态。

---

## 5. 微调工具链（已下载）

### 5.1 Qwen3 官方仓库 ★ 最权威

```
finetune/Qwen3/                HEAD 7a2f61f  (完整克隆，35.9 MB)
```

官方自己在维护的**训练文档就在这里**（这是你要的"官方微调资料"）：

| 文档 | 内容 |
|---|---|
| `docs/source/training/ms_swift.md` (14.8 KB) | ★ 阿里自家框架，Qwen 一等公民 |
| `docs/source/training/llama_factory.md` (4.9 KB) | LLaMA-Factory 路径 |
| `docs/source/training/axolotl.md` | Axolotl 路径 |
| `docs/source/training/unsloth.md` | Unsloth 路径（省显存） |
| `docs/source/training/verl.md` | verl（RL 方向） |
| `docs/source/run_locally/llama.cpp.md` (15.1 KB) | ★ llama.cpp 部署，与你的框架直接相关 |
| `docs/source/quantization/llama.cpp.md` | 量化说明 |
| `docs/locales/zh_CN/**/*.po` | ★ **上述文档的中文版**（含训练与 llama.cpp 章节） |
| `Qwen3_Technical_Report.pdf` (659 KB) | 技术报告 |
| `examples/llama-factory/*.yaml` | ★ 可直接改用的 SFT 配置：full / lora / qlora / merge |

### 5.2 ms-swift（阿里官方微调框架）

```
finetune/ms-swift/             HEAD 4967fd9  (浅克隆，40.9 MB)
```

理由：与 Qwen 同厂，Qwen3 支持最及时，官方文档 `ms_swift.md` 讲的就是它。**推荐作为主力**。

### 5.3 待装（按需）

- **LLaMA-Factory**：生态最大、文档最全、图形界面友好。官方 Qwen3 仓库里直接给了它的 yaml 示例（`examples/llama-factory/`），所以即使不装框架，示例也能先看。需要时：`git clone --depth 1 https://github.com/hiyouga/LLaMA-Factory.git`
- **Unsloth**：4 GB 显存场景下最省显存的选项，值得优先评估。
- **peft / trl / bitsandbytes / datasets**：以库形式安装，见 `微调环境搭建.md`。

---

## 6. ⚠️ 硬件现实：4 GB 显存是硬瓶颈

实测本机：

```
GPU : NVIDIA GeForce GTX 1050 Ti
VRAM: 4096 MiB
算力: compute_cap 6.1 (Pascal)
驱动: 582.66
```

这对你的计划有**直接且严重**的影响，必须现在就知道：

| 任务 | 4 GB 显存可行性 |
|---|---|
| Qwen3-0.6B **全参 SFT** | ❌ 不可行。bf16 权重 1.2 GB + 梯度 1.2 GB + Adam 状态 ~4.8 GB ≈ 7.2 GB 起 |
| Qwen3-0.6B **LoRA** | ⚠️ 勉强。权重 1.2 GB + LoRA 优化器状态，短序列下可能塞得进 |
| Qwen3-0.6B **QLoRA (4bit)** | ✅ **推荐起点**。4bit 权重约 0.4 GB，是最现实的路线 |
| Qwen3-**4B 全参** | ❌ 完全不可行（约 48 GB 起） |
| Qwen3-4B **QLoRA** | ⚠️ 权重 4bit ≈ 2.4 GB，训练时激活会紧张，需要短序列 + 梯度累积 |
| Qwen3-4B 推理（你的线上场景） | ✅ 你已经在跑更大的 27B Q2，4B 没问题 |

**这与规范 §11 的能力清单有冲突**：§11.1 要求训练的是"生成中主动回头检查""错误归因""写手+审稿人双能力""最小改动""尾部自审服从性"——这些**都依赖较长序列上的行为塑造**。长序列 + 大 batch 正是 4 GB 显存最承受不住的组合。

**三条可选路径**（建议在写新规范时一起定）：

1. **本机 QLoRA 小规模验证**：0.6B + QLoRA + 短序列，先证明"数据管道 + 训练脚本 + 评测闭环"跑得通。**收益有限但成本最低**，且能把流程全部打通。
2. **租云 GPU**：单卡 24 GB（4090/A5000 级）即可舒服做 0.6B/4B 的 LoRA 甚至 4B QLoRA。按小时计费，做对照实验比买卡划算得多。
3. **CPU 训练**：技术上可行但慢到不实用，不建议。

**建议**：先用路径 1 打通全链路（这也符合你"先铺路"的说法），同时把路径 2 作为真正的对照实验执行环境。

另外 `nvcc` **已存在**（`D:\Program Files\bin\nvcc.exe`），但 `cmake` **不在 PATH** 上 —— 要自行编译 ik_llama.cpp 得先补 cmake，或用 VS 2022 自带的。E 盘备份里有 `_tools\cmake.zip` 和 `cuda_12.9.0_windows.exe` 可直接用。

---

## 7. 建议的执行顺序

1. **先写新规范**（§0 所述），把 KV 操作与后训练正式纳入范围，定判据。
2. **打通微调闭环**：0.6B + QLoRA + 官方 yaml 改一版，跑通 sft → merge → gguf → 推理，确认链路。
3. **KV 协议先行**：在 ik_llama.cpp 上加 `/kv/*` 端点，**先用现成 API（`seq_rm`/`seq_add`）**，不重写底层。用 0.6B 验证"KV 编辑后上下文是否一致"。
4. **下载 4B 对照模型**，建立 A/B 基线（微调前）。
5. **数据构造**：按 §11.1 五类能力（A–E）设计数据集，注意 §11.1 每条都写了"训练数据形态"。
6. **后训练 + 对照实验**：0.6B / 4B，微调前后 × harness 开闭，四格对照。
7. **回流 harness**：把 §11.2 的三个环节（修订栈主动触发分布、多轮修订链、审核 Agent 专业化）接回既有 harness。

---

## 8. 版本与来源

所有 commit hash、文件 hash、来源 URL 见 `版本与来源清单.md`。
环境与依赖、4B 模型下载命令见 `微调环境搭建.md`。
与现有规范的冲突与建议见 `规范缺口与阶段定位.md`。
