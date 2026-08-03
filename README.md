### Context Engineering v2 — Anthropic 压缩流水线 + 分层混合记忆

> 参考 Claude Code 四级压缩流水线（Snip → Microcompact → Auto-Compact → Reactive），按成本从低到高组成兜底链路；跨会话经验复用通过 VectorDB + retriever（agent 主动触发的 tool）实现。

> 核心哲学先记住一句话：能靠规则删的先删，能靠磁盘文件托住的别动 LLM，真到无路可走才全量压缩

#### 一、三层记忆架构

| 层级 | 内容 | 生命周期 | 注入方式 |
|---|---|---|---|
| L0 工作记忆 | 最近 N 轮原始消息（高保真） | 会话内 | 消息尾部直接注入 |
| L1 情景摘要 | auto-compact 滚动摘要（增量 ≤300 词） | 会话内 | SystemMessage 顶部 `## CONVERSATION SUMMARY` |
| L2 长期记忆 | 关键决策 / 工具结果 / artifacts / 用户意图（向量化） | 跨会话 | retriever tool 按需检索注入 |

分层铁律：
- L1 只做增量合并，永不重新抽象已压缩段（`compaction_checkpoint` 游标保证）
- 重要事实（文件路径、修复记录、命令输出）原样抄送 L2，不依赖摘要转述

#### 二、热路径压缩流水线（每次 LLM 调用前，按成本从低到高依次运行）

**T0 Snip — 头部规则裁剪（零 LLM 成本，每轮跑）**
- 头部删除最旧消息（保留 SystemMessage 与首条任务消息），释放的 tokensFreed 传递给 T2 阈值判断

**T1 Microcompact — tool 输出清扫（零 LLM 成本，每轮跑）**
- 已过保留窗口的 ToolMessage 内容替换为占位符 `[content cleared]`，保留消息结构与 AI↔Tool 配对
- 工具层同时做输出预算截断：bash stdout 保头尾、write_file 只留 path+diff 摘要、read_file 限行数

**T2 Auto-Compact — 增量摘要（小模型 deepseek，阈值触发）**
- 触发阈值：`compact_threshold = budget - MAX_SUMMARY_OUTPUT - BUFFER`
- 只压缩 `compaction_checkpoint` 之后的中段：`new_summary = merge(existing_summary + 新消息)`
- 已压缩原文用 RemoveMessage 删除；摘要注入 SystemMessage 顶部
- 工程保障：熔断（连败 3 次降级为纯 Snip）、递归保护（压缩执行中禁止再次触发）、压缩在节点边界异步执行，绝不在 streaming 过程中运行

**T3 Reactive — 兜底（收到 400/413 超限错误才触发）**
- 紧急压缩 → 自动重试原请求（最多 1 次）

#### 三、预算分配（可配置常量）

| 角色 | budget | MAX_SUMMARY_OUTPUT | BUFFER |
|---|---|---|---|
| orchestrator | 60k | 2k | 13k |
| sub-agent | 40k | 1.5k | 13k |

#### 四、冷路径：编排完成后写入 VectorDB

orchestration 完成后：提取关键信息（AI 决策、工具结果摘要、artifacts 清单、用户意图）→ embedding → 入库 VectorDB，按 conversation_id / orchestration_id 分桶。

#### 五、跨会话复用：retriever 是 agent 主动触发的 tool

- orchestrator 新增 `retrieve_memory` 工具（参数：query / top_k / 时间过滤），检索由 agent 主动调用，非自动注入
- 第一轮不检索：当前 conversation 的 vectordb 为空
- 检索结果注入为 SystemMessage 段 `## RELEVANT EXPERIENCE`（≤1k tokens）

#### 六、根因映射（对应下文 1-4）
13k 缓冲：不主动触发，是阈值减项——保证压缩器自己调用时有空间
| 根因 | 解决方案 |
|---|---|
| 1. LLM 节点无 trim/summarize | T2 Auto-Compact 挂载 orchestrator / llm 节点边界 |
| 2. Tool 输出无预算截断 | T1 Microcompact + 工具层输出预算截断 |
| 3. sub_agent_outputs 全文回流 | summarize_node 返回结构化摘要（final_text 截断 + artifacts 清单），不再全文进 messages |
| 4. 迭代上限偏松 | 无进展早停：连续 N 轮相同工具+相同参数 → 强制总结 |

1. LLM 节点无 trim/summarize：每次调用发送完整累积历史（含全部 ToolMessage 全文）
2. Tool 输出无预算截断：bash stdout、文件 diff、artifact 报告等大输出原样进入消息历史，且永不淘汰
3. sub_agent_outputs 全文回流：fanout 结果合并后，orchestrator 下一轮把完整交付报告再次重发
4. 迭代上限偏松：MAX_ITERATIONS=50（orchestrator）/42（子代理），且无"无进展早停"机制

#### 七、Example Dry Run:
```markdown
conversation_id: conv_id_crazythursdayvivo50kfc

  orchestration_id: orch_id_runrun001          ← 首次编排：vectordb 为空 → retriever 不触发
  │  第 1 轮  [请求前hook] 预算检查 messages=2.1k ≤ 45k → 无动作，直接 LLM
  │  第 3 轮  [请求前hook] T0 Snip 检查：头部无冗余(plan/fanout 均未过期) → 跳过
  │  第 4 轮  fanout 3 个 programmer（写文章）→ Send 子图
  │  ├─ programmer_1 ReAct 第 5 次迭代前 [请求前hook]
  │  │     T0 Snip: 无冗余 → 跳过
  │  │     T1 Microcompact: bash ToolMessage 已过保留窗口(≥3轮) → 内容替换 [content cleared]
  │  │     预算检查: 23k ≤ 25.5k (sub-agent budget 40k-1.5k-13k) → 不触发 T2
  │  ├─ programmer_2 / programmer_3 同理
  │  第 9 轮  [请求前hook] 预算检查: 46.5k > 45k → 触发 T2 Auto-Compact
  │           compaction_checkpoint=msg_8.id → 只压缩 (msg_4, msg_8] 中段（不重复压缩 1-3）
  │           调 deepseek(max_tokens=512) → 摘要 0.4k → 注入 SystemMessage 顶部
  │           递归保护: compact_in_progress=true → 摘要调用自身不再触发压缩
  │           RemoveMessage 删除 12 条原文 → messages=9.6k（摘要 0.4k + 最近 8 条）
  │  第 15 轮 熔断示例: T2 连续 2 次压缩失败(摘要模型超时) → 第 3 次触发熔断
  │           剩余流程降级: 仅 T0/T1 规则裁剪，不再调 LLM 压缩
  │  第 17 轮 T3 Reactive 示例: LLM 调用抛 413 (fetch_web 输出一次性推过 60k)
  │           → 紧急 Auto-Compact(忽略熔断，仅此 1 次) → 重试原请求成功
  │  第 20 轮 end_orchestration → 冷路径:
  │           提取 AI决策/工具结果摘要/artifacts/用户意图 → embedding
  │           → 入库 VectorDB (key: conv_id_crazythursdayvivo50kfc / orch_id_runrun001)

  orchestration_id: orch_id_runrun002          ← 检索到 001 的经验
  │  第 1 轮  orchestrator 主动调用 retrieve_memory(query="写文章", top_k=3)  ← agent 主动，非自动注入
  │           ← 命中 001 的 2 条经验 → 注入 SystemMessage 段 ## RELEVANT EXPERIENCE (≤1k)
  │  第 6 轮  [请求前hook] T1 Microcompact: fanout 的 ToolMessage 已过保留窗口 → 清空
  │  第 13 轮 预算检查: 47k > 45k → T2 Auto-Compact (增量，checkpoint 从未压缩段开始)
  │  完成 → 冷路径入库 → VectorDB (key: conv_id_crazythursdayvivo50kfc / orch_id_runrun002)

  orchestration_id: orch_id_runrun003
      ← 同 002，启动时检索 001+002 的经验 → 注入初始上下文
      ← 流式中重复 T0/T1/T2 流程...
```
