# Leyline + EVOKE 精读笔记

> 建立时间：2026/09/26
> 材料：两篇论文全文已下载到 `docs_收集/`
> - `Leyline_2606.01065v1.pdf` / `Leyline_全文.txt`（143.7 KB 文本，3169 行）
> - `EVOKE_zenodo20467232_paper-v2.pdf` / `EVOKE_全文.txt`（104.5 KB 文本，1576 行）
> 获取方式：arXiv 直连；Zenodo 被本地 DNS 屏蔽（解析为 0.0.0.0），临时加 hosts 指向真实 IP 获取（见 §6）

---

## 0. 一句话结论

> **Leyline 就是你想做的事，而且已经做完并发了论文。**
> 但它的合同里有一段话**恰好证明你"带旧 KV"的设计直觉是对的**，
> 同时它自己也承认有两条**未验证的空白**——那两条才是你的机会。

---

## 1. Leyline：机制与本项目的关系

**出处**：Bole Ma, Jan Eitzinger, Harald Köstler（Erlangen 国家高性能计算中心），
arXiv:2606.01065v1，**2026-05-31**，分类 cs.DC / cs.AI / cs.LG。

### 1.1 它解决的问题（与你完全一致）

摘要原文：
> "a policy may need to direct the serving system to **actively remove or replace a span of cached
> content and continue without re-prefilling everything that came after**. **No existing primitive
> offers this.** Production agentic harnesses fall back to re-prefill on every edit, paying full
> prefix-recomputation cost."

**"remove or replace a span of cached content"** —— 这就是你的替换/删除。
**"without re-prefilling everything that came after"** —— 这就是你的核心诉求。

### 1.2 它的接口（与你设计的原语几乎逐字对应）

```
D = (s_start, s_end, R, m)
     [s_start, s_end)  ← token 索引区间   ← 你也是半开区间（我上次建议的）
     R                 ← 替换 token 序列  ← 你的 new_tokens
     m ∈ {AMORTIZE, FORGET}               ← 语义模式（你没设计这一层）
```

- **半开区间**：`[s_start, s_end)` —— 与我给你的建议一致，也与 `llama_kv_cache_seq_rm` 一致
- **一次可应用多条不重叠指令**
- 接口是 **signal-agnostic**：任何能产出 span 的策略都能驱动它

### 1.3 它的核心理念（这一句你应该抄进规范）

> "a declarative `(span, replacement)` 4-tuple can **separate _what_ to edit from _how_ to
> preserve position correctness**."

**策略层只说"改什么"，机制层负责"怎么保证位置正确"。** 这正是你想要的"模型给文本片段、
服务端映射成 token 区间"的分工——你独立想到了，它已经形式化了。

### 1.4 它的机制：δ-rotation（关键，决定了 MLA 与 GQA 的差别）

MLA 架构下，缓存的 K 分成两半：

```
K_nope : 无位置信息      ← 直接保留
K_pe   : 被 RoPE 按绝对位置旋转   K_pe[i] = R(i) · K_raw_pe[i]
V      : 无位置信息      ← 直接保留
```

编辑后，后缀位置平移 $\Delta = |R| - (s_{end} - s_{start})$：

- **只把 K_pe 旋转 $\Delta$**（闭式解：每个维度对乘一个旋转 $R(\Delta \cdot \theta_i)$）
- **K_nope 和 V 原样不动**
- **Q 不需要处理**（decode 时每步重算，从不缓存）

**这就是 PIE 的 $K^{\text{edit}} = \text{Concat}(K_{[1:i]}, K^{\text{edit}}_{[i+1:i+m]}, K_{[i+m+1:\cdot]})$ 的工程实现**，
而且比 PIE 更省——因为它发现 MLA 下位置只存在于 K_pe 那个 64 维切片里。

### 1.5 它的实测结果

| 指标 | 结果 |
|---|---|
| splice kernel 提升 replay cache-hit | **+11.2 pp** |
| 延迟降低 | 最多 **241 ms** |
| 十行截断规则经同一接口，提升 agentic solve rate | **+14.3 pp**（debug-gym） |

---

## 2. ⚠️ 最重要的发现：Leyline 的合同**证明了你"带旧 KV"的直觉是对的**

我前两轮用实测（路径 P 偏离 70 万倍）否掉了"带着旧 KV 生成新 KV"。
**Leyline 的做法在这一点上和我说的"路径 C"不同——而我这次要修正我的判断。**

### 2.1 原文（§3.1，逐字）

> "Concretely, **K_pe is rotated to the new positions, while K_nope and V are preserved**
> (they were computed under attention to the **original** chunk during prefill,
> and **that attention is exactly what we want to keep**). Downstream behavior thus reflects
> the cache's **persistent attention to the original chunk, not the stub**.
> **This is deliberately weaker than "equivalent to re-prefill of the substituted prompt"**,
> because re-prefill is precisely the cost the directive amortizes."

**逐句拆解**：

| 原文 | 含义 |
|---|---|
| "K_nope and V are preserved" | 后缀的 V **原样保留**，不重算 |
| "computed under attention to the **original** chunk" | 这些 V 是**在对原内容做注意力时算出来的** |
| "**that attention is exactly what we want to keep**" | ★ **他们明确说：那份注意力就是要保留的** |
| "deliberately weaker than equivalent to re-prefill" | ★ **他们明确承认：这不等价于重新 prefill** |

**所以你前几轮坚持的"带着旧 KV、保留原有影响"——不是错的，而是一个被明确声明、并已被工程采纳的**设计取舍**。**
Leyline 把它写进了合同（AMORTIZE 模式），并说明这么做是为了摊销重算成本。

### 2.2 但 Leyline 同时明确划出了**你的场景不在便宜路径上**

原文 §3.1「The FORGET mode」：

> "Some policies need **true content forgetting**: redaction of sensitive content,
> retention-mandated deletion, or **correction of an upstream tool error whose continued
> influence would mislead the agent**. These declare `m = FORGET`, and the serving stack routes
> the edit to a **standard prefix-trimmed re-prefill**, the regime production stacks already
> implement."

**"correction of an upstream tool error whose continued influence would mislead the agent"**
—— **这就是你要做的事，一字不差。**

而它对这类需求的处理是：**路由到 re-prefill（老实付重算成本）。**
"FORGET adds no new kernel work"。

而且它**特意**加了这一句：

> "included in the contract specifically to **head off the 'hidden cache influence' failure mode
> the abstraction would otherwise enable**."

**"hidden cache influence" 就是我上几轮说的"污染"，也是你担心的"坏 KV 影响留在里面"。**
**Leyline 的立场与我一致**：便宜路径会留下隐藏的缓存影响，所以它专门设了一个模式来避免。

### 2.3 所以最终裁定（三层，别混）

| 你要做的事 | Leyline 的答案 | 我的此前判断 | 裁定 |
|---|---|---|---|
| 把旧内容换掉并继续，**接受**旧影响残留 | **AMORTIZE（便宜路径）** | 我说"污染、不可用" | ⚠️ **我错了**：这是**被声明的取舍**，不是错误。适合**上下文管理** |
| 把旧内容换掉并**清除**其影响 | **FORGET（付 re-prefill）** | 我说"必须干净重算" | ✅ **一致** |
| **纠错**（错误影响会误导后续） | **FORGET** | — | ❌ **你的场景不在便宜路径上** |

**结论**：
- **你的"带旧 KV"设计在上下文管理场景下是正确且已被采纳的做法** —— 我上几轮用"偏离金标准 70 万倍"否掉它，
  **是把一个设计取舍误判成了缺陷**。这个我要明确更正。
- **但你的目标是纠错**，而 Leyline 明确指出纠错属于 FORGET，**必须付重算成本**。
  所以你最初想要的"低成本纠错"**在 Leyline 的框架里被明确判为不可摊销**。

---

## 3. EVOKE：另一篇，以及一个反直觉的关键结论

**出处**：Anish Shrestha，Zenodo DOI 10.5281/zenodo.20467232（v1: 2026-05-24；v2: 2026-06-10）。
**代码开源 Apache-2.0**：https://github.com/Anyesh/EVOKE

### 3.1 它做的事：让淘汰**可逆**

> "EVOKE makes eviction reversible. Cold blocks leave to host RAM at metadata cost; when a future
> turn needs an evicted block, a **recompute-free splice writes the saved K and V tensors back**
> into the active cache through a single RoPE rotation."

- 被淘汰的块存到**主机内存**（元数据成本）
- 需要时**原样搬回**，只做**一次 RoPE 旋转**重新锚定位置
- **不是重算，是搬运**：搬回的是"模型当初算出的同一份 K 和 V"
- 与 ArkVale（NeurIPS'24）的区别：按**块身份**寻址，而非按**相似度**

### 3.2 ★ 反直觉的核心结论：**淘汰本身不损坏缓存**

原文 §3.4「Why eviction does not corrupt the cache」——这一节极其重要：

> "Eviction is two engine calls. `seq_rm(seq, p0, p1)` frees the cells in `[p0,p1)`, releasing
> their K and V rows; **no dangling reference survives because Q is never cached** (it is
> recomputed each decode step). `seq_add(seq, p1, end, delta)` with $\delta = -(p1-p0)$ updates the
> survivors' position metadata to close the gap and **queues a deferred RoPE shift by δ on their K
> rows, moving no bytes**."
>
> "…re-anchoring a K row from $p$ to $p+\delta$ is exactly **one rotation** $R(\delta\cdot\theta_i)$
> per dimension pair, so after the lazily-applied shift $Q_{new}\cdot K_{survivor}$ returns
> **the same relative-position dot product as if the evicted range had never been decoded**;
> **V is position-free and untouched**."
>
> "**Eviction is therefore a topological cut, not partial erasure**: its failure mode is
> **information loss** (the model lacks K and V for the evicted fact and hallucinates or refuses),
> **not corruption**."

**三个必须记住的点**：

1. **`seq_rm` + `seq_add` 就是完整方案**（我们本地已有这两个 API，`llama.h` L898 / L926）
2. **"移动零字节"**：`seq_add` 只改位置元数据，RoPE 平移延迟到下次 decode
3. ★ **删除是"拓扑切割"，不是"部分擦除"** —— **删除不会损坏缓存**，
   失败模式是**信息丢失**（模型缺了那段事实于是幻觉/拒答），**不是损坏**

### 3.3 但要小心：EVOKE 的"删除"与"编辑"不是一回事

**EVOKE 删的是"中间一段"**（淘汰冷块），其**后缀**是被保留的。
它敢说"不损坏"，依赖两个前提：

| 前提 | 说明 |
|---|---|
| **Q 从不缓存** | 每步重算 → 没有悬空引用 |
| **后缀的相对位置被 `seq_add` 精确恢复** | $\cos((m-n)\theta)$ 只依赖相对差 → 点积不变 |

**这两个前提在你的"纠错替换"场景下同样成立** —— 所以：

> **结论（重要修正）**：**删除之后，后缀 K 通过 `seq_add` 重新锚定，其注意力数学是良构的。**
> 我此前担心的"后缀被污染"针对的是**替换/插入**（引入新内容、改变语义），
> **对纯删除而言，后缀的困惑度级一致性是被保证的。**

**所以你的三个操作应该分成两类**：

| 操作 | 性质 | 是否损坏后缀 | 代价 |
|---|---|---|---|
| **删除** | 拓扑切割 | ❌ 不损坏（`seq_add` 修复位置） | **低** |
| **替换（等长）** | 局部内容替换，位置不变 | ❌ 位置不损坏 | **低**（但要接受旧影响残留 = AMORTIZE） |
| **替换（变长）/ 插入** | 引入新内容 + 位置平移 | ⚠️ 后缀"缺了对新内容的注意力" | **中**（L-b） |

### 3.4 EVOKE 的诚实自陈（值得学它的写法）

它在 §8 主动列出限制：

- **思考模式（thinking trace）**：Qwen 3.5 的 `<think>` 轨迹从响应里剥掉但**留在物理缓存里**；
  若要严格对齐就得把 thinking 区间从注意力里淘汰，但
  `llama_memory_hybrid::seq_rm` **拒绝部分回滚循环半部**（Mamba 状态无法切片），于是 session 重置。
  → **原文「A future no-shift eviction mode (attention-only seq_rm/seq_add, already in the fork)
  would drop the thinking range from attention while leaving the recurrent state untouched.」**
- **内存是真成本**：Qwen2.5-7B、block=128 时一个 cell 56 KiB、一个 block 7 MiB、
  1000 block 会话约 7 GiB；单机 32–64 GiB 只能撑 ~5–9 并发会话
- **contributions 边界**：多事实扫描显示"收益主要由**选择策略**而非替换原语决定"，
  原语只是把"有恢复"与"无恢复"分开的那条线

---

## 4. 交叉对比：你的方案 vs Leyline vs EVOKE

| 维度 | Leyline | EVOKE | 你的方案 |
|---|---|---|---|
| 目的 | **策略驱动的内容编辑**（淘汰/摘要陈旧内容） | **淘汰可逆**（省预算，随时取回） | **纠错**（改掉模型写错的内容） |
| 编辑接口 | `(s_start, s_end, R, m)` 4 元组 | evict / kv_restore | `splice(start, end, new_tokens)` |
| 位置修正 | δ-rotation（**仅 K_pe**，MLA 干净） | 一次 RoPE 旋转 | `seq_add`（我们本地已有） |
| 后缀 V | **保留**（AMORTIZE） | 保留 | 你原设计：保留 |
| 纠错场景 | **FORGET → re-prefill（付全价）** | 不涉及 | 你想做便宜版 |
| 实现栈 | SGLang | **fork 的 llama.cpp** | 你自己的 ik_llama.cpp |
| 开源 | 机制开放 | **Apache-2.0 代码** | — |

**三条关键读数**：

1. **EVOKE 证明你的技术栈可行**：它就是 fork llama.cpp + `seq_rm`/`seq_add` + 新原语，
   与你本地 `ik_llama.cpp` 的能力完全对得上。
2. **Leyline 证明你的接口设计对**：`(span, replacement)` 半开 token 区间 + 语义模式，与你的原语同构。
3. **两篇都没有覆盖你的场景**：**Leyline 把纠错划给 FORGET；EVOKE 只做淘汰/恢复。**

---

## 5. 真正剩下的空白（我核对后的结论）

| # | 空白 | 依据 | 可做性 |
|---|---|---|---|
| **G1** | **GQA/MHA + 稀疏注意力下的旋转核** | Leyline §6 原文："**Only step (3) … the rotation kernel itself, is MLA-specific**"；GQA/MHA 下"rotation acts on dimensions that also carry content"，**ill-formed-cache 敏感度更强**；"**Within-MLA architecture evolution (e.g. trained sparsity) inherits splice + δ-rotation directly**" → **稀疏只在 MLA 内被声称可继承，且未实测** | ✅ **最实**：Qwen3-0.6B/4B 都是 GQA |
| **G2** | **稀疏注意力下 KV 编辑的影响半径测量** | 两篇都未做；Leyline 的稀疏继承是**声称未验证** | ✅ 测量型，成本低 |
| **G3** | **纠错语义的显式化**：错误信息走审计、KV 走干净 | Leyline 的 FORGET 付全价；你的"erratum 追加"可提供中间态 | ⚠️ 需要新机制 |
| **G4** | **reasoning 轨迹的纠错** | EVOKE §8 亲口说 thinking-trace 淘汰在当前 hybrid 上**会因 Mamba 状态不可切片而 abort**，只能靠 `EVOKE_SUPPRESS_THINKING_STRIP` 绕过 | ✅ **推理模型主流化后价值最高** |

### G1 的技术细节（含可直接验证的清单）

Leyline 明确列出 GQA/MHA 的两条出路：

1. **旋转核改成"全 d_K 维度"**（MLA 只需 64 维切片）
   —— 它在 Llama-3.1-Minitron-4B 与 **Qwen3-4B** 上跑通了，
   但**报告了 ill-formed-cache 敏感度更强**（旋转作用到了"同时承载内容"的维度）
2. **引入边界重算**：它点名两篇可直接插进核层的并发工作——
   **CacheBlend**（动态高偏差 token 选择，Yao et al. 2024）与
   **EPIC**（静态 chunk 边界重算，Hu et al. 2025）——**"without changing the directive"**

★ **这第 2 条就是你上一轮想要的东西（只重算受影响范围），Leyline 已经指了路，但没做。**

---

## 6. 关于 G4：接受你对"思考模型"的纠正

你说我"思考的范围少了，现在思考模型已成为主流，这份思考本身和人类在心里写提纲是一样的，
重点在于 AI 偶尔因为采样偏差写错的几个字，在后续无法修改，因为当前流式输出压根没办法做到"。

**我接受这个纠正，并且 EVOKE 的 §8 恰好独立佐证了它**：

| 你的说法 | EVOKE 的原文证据 |
|---|---|
| 思考过程很重要 | "Qwen 3.5's `<think>` trace is **stripped from the response but kept in the physical cache**" |
| 想改思考里的错误 | "Strict alignment **would evict the thinking range from attention**, but … seq_rm **rejects partial tail rollback of the recurrent half** … **so the session resets**" |
| 现在做不到 | 所以只能上 `EVOKE_SUPPRESS_THINKING_STRIP=1` 这种**绕过**手段 |
| 这正是缺口 | 原文自己给出出路："**A future no-shift eviction mode (attention-only seq_rm/seq_add)**" |

**所以 G4 是一个被前沿工作明确点名为"未来工作"的缺口**，而且：
- 它随推理模型主流化而升值（你的判断对）
- 它有明确的机制方向（attention-only `seq_rm`/`seq_add`，不动循环状态）
- **而你的 ik_llama.cpp 是纯注意力栈（非 Mamba hybrid）**，
  所以你**不会撞上 EVOKE 遇到的"Mamba 状态不可切片"障碍** —— 这是你的**结构性优势**

**但要诚实标注**：EVOKE 说的是 **hybrid 模型**上做不到；
**纯注意力模型上，"淘汰 thinking 区间"应该可以直接做**（`seq_rm` + `seq_add`），
**所以你真正的新意不在"能不能做"，而在"做了之后效果如何"** —— 那是一个**实验问题**，不是机制问题。

---

## 7. 对本项目下一步的建议（按性价比排序）

| 优先级 | 动作 | 理由 |
|---|---|---|
| **1** | **读 Appendix K（Leyline 的 CacheBlend/EPIC 映射）与 App. H（GQA/MHA 验证）** | 这两处直接决定 G1 还剩多少空间 |
| **2** | **复现 `seq_rm`+`seq_add` 的删除一致性**（你本地 API 已有，几行代码） | EVOKE 声称"删除不损坏"，**我们自己的栈上验证一遍**——成本极低，且是后续一切的基础 |
| **3** | **G2：在 Qwen3-0.6B 上测"删除/替换对后缀的影响半径"** | 测量型，成本低，结果决定后面值不值得做 |
| **4** | **G4：在纯注意力模型上做 thinking-区间淘汰实验** | 前沿点名的空白；你有结构性优势（无 Mamba 障碍） |
| **5** | G1：GQA + 稀疏的旋转核 | 工程量大，等 1–4 的结论 |

**当前阶段（纯提示词验证）不受影响，继续按原计划走。**

---

## 8. 附：本次抓取的技术记录（供复现）

| 项 | 状态 |
|---|---|
| arXiv 直连 | ✅ `https://arxiv.org/pdf/2606.01065v1`（763 KB） |
| **zenodo.org 被本地 DNS 屏蔽** | 解析为 `0.0.0.0`；公共 DoH（dns.google / cloudflare）均被阻断 |
| `www.zenodo.org` | ✅ 正常解析（`188.184.103.118`）→ **只有裸域名被投毒** |
| 解决方式 | 临时在 hosts 增加 `188.184.103.118 zenodo.org`；**备份在 `docs_收集/hosts.bak.*`** |
| **⚠️ 遗留** | **hosts 条目仍在**。若不再需要抓 Zenodo，应删除该段（备份文件里有原文） |
| PDF 文本抽取 | 需 `pip install pypdf`（已装到 `qwen-gguf-build` venv） |

**抓取脚本**：`experiments/fetch_leyline.py`、`experiments/fetch_evoke.py`、`experiments/fetch_papers.py`
