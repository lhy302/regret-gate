# open_questions.md — 实现中发现的规范未覆盖细节

> 依据构建规范 §17：**规范未覆盖的工程细节先记录在此，按最保守方案实现，不自行扩展实验范围。**
> 每条都写明“规范哪一句没覆盖 / 本文档选了什么最保守方案 / 为什么 / 影响范围”。

---

## Q1. 提交触发信号（v4.1 §3.5 要求明确，构建规范 v1.0 未定义）

- **缺口**：v4.1 §3.5 明确要求“提交触发信号必须明确”（`commit` 工具 / `<<COMMIT>>` 标记 /
  不再提交新 `ctx_*` 并以纯文本结束 / 超时），且 §8 验收 #23 要求验证无歧义。
  但构建规范 v1.0（最高权威）没有定义任何提交信号：它只规定“尾部自审后进入 finalize”。
- **最保守方案**：同时实现 v4.1 §3.5 给出的四个候选信号中最不引入新协议的三个，
  且**不新增工具、不改 messages 协议**：
  1. 模型调用 `draft.commit_revision` → 立即登记为 pending（等价“调用 commit 工具”）；
  2. 模型在尾部自审窗口内不再提交新修订、以纯文本结束 → 进入 finalize；
  3. 尾部自审超时（默认 30s）→ 记 `tail_audit_timeout` 后进入 finalize。
- **未采用**：`<<COMMIT>>` 文本标记。它要求模型输出特定字面量，属于“依赖模型自发行为”，
  与铁律 4 冲突（机制必须由 harness 强制），且会给 `system_prompt` 增加与
  `prompt_condition` 无关的内容，违反铁律 8。
- **影响范围**：`core/tail_audit.py` 的循环终止条件、`experiments/harness.py` 的 finalize 时机。

## Q2. `tail_audit_modified` 的判定口径与“基础设施失败”区分

- **缺口**：规范 §13.1 要求“尾部自审触发修改率 = `tail_audit_response` 中调用
  `draft.commit_revision` 的比例”，但没定义**尾部自审根本没被调用**时怎么记。
- **最保守方案**：三态区分，避免把链路缺失误算成“模型不服从提示”（那会直接误判 H4）：
  1. `tail_audit_modified = False`：尾部自审确实执行了，模型没登记修订；
  2. `tail_audit_timed_out = True`：等待超时，按“无修改”处理（规范 §7.5 明文）；
  3. `tail_audit_infrastructure_error = True`：机制被开启但一次都没注入成功 —— 这是
     harness 自身故障，必须显式暴露，不能摊进“模型不服从”。
- **影响范围**：`experiments/metrics.py`、`experiments/analysis.py` 的 H4 判定。

## Q3. `H` 组 `revision_stack` 与尾部自审的联动

- **缺口**：§11.2 矩阵中 C 组 `revision_stack = ✗`，但 §7.5 又要求“模型调用
  `draft.commit_revision` → 入 RevisionStack”。两者在 C 组语义上互相冲突：
  若 C 组真的没有修订栈，尾部自审登记的修改无处落地，C 组测的就不再是“尾部自审效果”。
- **最保守方案**：**配置严格照抄矩阵（C 组 `revision_stack=false`）**，
  修订栈仅作为尾部自审的落地协议层（`revision_stack` 开关只决定“登记到草稿的修改是否
  被应用”），不把它当作独立实验变量。这样 C 组相对 A 组的唯一差异仍是“尾部自审是否注入”，
  对齐 §0.2「四项机制」中“尾部强制自审”的定位，也不违反铁律 8。
- **影响范围**：`configs/groups/C_tail_audit.yaml` 注释、C 组指标解释。

## Q4. `insert` 操作的插入位置

- **缺口**：§3.2 的 `draft.commit_revision` schema 只有 `target_text`/`op`/`payload`，
  没有“插在目标之前还是之后”的字段；§6.2 的 finalize 算法也只笼统写“应用”。
- **最保守方案**：`insert` = 在 `resolve_range` 定位到的片段**之后**插入 payload，
  `target_text` 只做定位、不删除。理由：不动被定位的原文，改动范围最小、可预测，
  且与 `replace`/`erase` 共用同一套区间语义。
- **影响范围**：`core/revision_stack.py::_apply_one`、`tests/test_revision_stack.py`。

## Q5. 多回合 run 的定义（规范未定义“回合”边界）

- **缺口**：规范处处讲“每回合注入一次”“下一回合 payload 用 `finalized_text`”，
  但没定义一次 run 里到底有几个回合、每个回合的用户输入是什么。
- **最保守方案**：一次 run = **2 个回合**，第 2 个回合不引入新的用户输入，
  只验证“下一回合 payload 中 base_text 已被 finalized_text 替换”（§14.2 场景 2 的验收点）。
  回合数固定 → 不引入额外实验变量，且能覆盖跨回合 payload 的核心验收。
- **影响范围**：`experiments/harness.py::MAX_TURNS`、指标 `llm_calls` 的上界。

## Q6. 离线 `latency_ms` 的可复现性

- **缺口**：§14.3 要求记录真实延迟，§14.4 又要求“同配置连跑两次指标完全一致”。
  真实挂钟时间不可能逐字节一致，两者存在张力。
- **最保守方案**：**离线（FakeClient）运行注入固定步长时钟**（`DeterministicClock`），
  使 `latency_ms` 也确定；真实 API 运行使用 `time.monotonic`，`latency_ms` 为真实耗时。
  这样两份要求各自在自己的运行模式下都被满足，且无需在任何地方“假装”延迟。
- **已知残留**：审计日志的 `ts` 用真实时间，因此 `audit_log_bytes` 会有几字节浮动 ——
  它是日志体积观测量（性能验收用），不是实验指标；确定性用例显式排除该字段并另行
  断言其 < 5 MB。
- **影响范围**：`experiments/runner.py::run_single`、`tests/test_end_to_end.py::DeterminismTest`。

## Q7. 误拦截率的判定基准（规范给了公式，没给基准来源）

- **缺口**：§13.1 定义“误拦截率 = 无害操作被 intercept 的次数 / 总 intercept 次数”，
  但“无害”由谁判定没有定义。
- **最保守方案**：以任务自带的 `dangerous_operations` 标注为真值基准（§12.5 要求每个任务
  必须标注），并明确采用**会对自己不利**的口径：被判 intercept 的操作，只要其任一命令段
  操作名不在 `dangerous_operations` 里，就计为误拦截。因此“只读命令 + 路径越权”这类
  由路径规则触发的拦截会被计为误拦截并暴露在 B 组指标里。
- **影响范围**：`core/risk_router.py::is_false_intercept`、`experiments/analysis.py`。

## Q8. `unit_test` 类验证器的默认不可用

- **缺口**：§12.2 要求实现 `unit_test` 验证器（给定测试文件跑测试），§12.4 的示例任务用
  它做逻辑错误判定，但规范没有附带任何测试文件。
- **最保守方案**：验证器完整实现（真跑 `unittest`，通过环境变量把被测产物路径传给测试），
  但**任务集不声明任何不存在的测试文件**：本实验 80 个任务的验证分别是
  `compile`（long_code / mixed）与 `regex`（long_text / short_command）。
  若声明了 `test_file` 而文件不存在，该 run 记为**失败并写审计**，绝不伪造成通过。
- **影响范围**：`experiments/validators.py`、`configs/tasks/*.yaml`。

## Q9. 只开 `external_auditor`（D 组）时，拦截来源从哪来

- **缺口**：§11.2 矩阵里 D 组是「只有独立审核」，但 §8.1 又规定审核只对
  `PendingActions` 中的 `intercept` 级操作触发；而进入 `PendingActions` 需要 `RiskRouter`
  先分级。按字面实现，D 组会**一次审核都不发起**（没有 intercept 来源）→ 机制空转，
  D 组测不到任何东西，H5 永远无数据。
- **最保守方案**：把 `risk_router` 与 `external_auditor` 的职责分开：
  `risk_router=false` 表示“分级结果**不参与处置**（不对 buffer/intercept 做暂存后的差异化处理）”，
  而不是“不做判定”。因此当 `external_auditor=true` 时，harness 仍然调用 `RiskRouter.classify`
  以获得 intercept 集合交给审核 Agent；审计日志里用 `classified_for` 字段标明
  「本次判定是服务于 risk_router 还是 external_auditor」，保证可追溯、不混淆机制归因。
- **为什么这样最保守**：D 组相对 A 组的唯一差异仍然是「独立审核 Agent 是否对高危操作生效」，
  符合 §0.2「四项机制」中“独立审核 Agent”的定位，也不新增任何实验变量；
  反之若让 D 组空转，等于把 D 组当成 A 组的复制品，属于伪造实验分组。
- **影响范围**：`experiments/harness.py::_handle_tool_call`、
  `tests/test_mechanism_activation.py`（锁死“D 组必须真的发起审核、A/C 组一次都不发”）。

## Q10. 离线 FakeClient 对 `prompt_condition` 的响应

- **缺口**：H 组（提示词矩阵）与 G 组（强提示词）要求 `prompt_condition` 在**模型行为**上
  产生可观测差异，但离线 FakeClient 没有“模型”，不模拟响应就等于三个子组同结果，
  H 组矩阵失去意义（v0.2 §1 明说 H 组只测“能力边界”）。
- **最保守方案**：离线桩把 `prompt_condition != none` 映射为「生成期主动登记一条修订」
  （`spontaneous_revision`），且该修订是**等价修订**（`target_text == payload`，不改变文本）。
  于是 H-weak / H-strong 的 `revision_hit_rate` 与 `output_tokens` 会与 H-none 不同，
  但**语法错误率不变** —— 正好体现“有登记能力 ≠ 能修好”。
  同时缓存键包含该标志，保证三个子组的客户端实例不互相污染。
- **明确不做的**：不把“主动登记”写成能修好缺陷，否则会把“能力边界”（H 组）与
  “尾部自审的修复效果”（C/F 组）混为一谈。
- **影响范围**：`experiments/runner.py::build_offline_client`、`llm/fake_client.py`、
  `tests/test_end_to_end.py::Scenario3HMatrixTest`。

---

## 不构成 open question 的事项（已在别处处理）

- **v0.2 §2.4 的“用 `reason` 做语义近似匹配”**：与构建规范 §6.3「禁止模糊匹配」直接冲突。
  按权威顺序（v1.0 > v0.2）以 v1.0 为准，**未实现**语义匹配；匹配失败一律 `rejected`。
- **v4.1 的 KV cache / 从编辑点重算 / 下游偏移蒸发 / 前缀缓存失效**：v0.2 §0.1 已移出实验范围。
  本 harness 不做任何 KV / logits / hidden states 操作，只在 `metrics` 里记录
  `cache_break_point`（payload 最早变化位置）作为成本观测，不把它当作机制贡献。
