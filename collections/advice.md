
我看到了无限循环的模式。现在让我检查 `make_plan` 工具的实现和相关代码，以找出根本原因。
现在我已经完整分析了整个项目和日志。让我给出完整的分析。

---

## Bug 现象

从日志可以清晰看到：

- **7_30_005.log**：`programmer_1`（Backend Developer）在正常写了 requirements.txt、database.py、models.py、schemas.py、seed.py 之后，从第 169 行（15:42:20）开始，**每 ~3 秒调用一次 `make_plan`**，持续循环 50 次直到 `sub_agent_iteration_limit` 触发（第 341 行），最终用户 Ctrl+C 强制终止。
- **7_30_004.log**：同样的模式，两个 programmer sub-agent 都陷入 `make_plan` 死循环。

---

## 根因分析

这个 bug 是 **三个问题叠加** 造成的，单一问题不会致命，但三者叠加形成死循环：

### 根因 1（核心）：`trim_messages` 截断破坏了对话上下文

[llm.py](file:///Users/shenweizhang/Desktop/multi_agent_orchestration_v0.0.2/orchestration/workers/nodes/llm.py#L78-L86) 中：

```python
messages = trim_messages(
    all_messages,
    strategy="last",
    token_counter=count_tokens_approximately,
    max_tokens=6000,
    start_on="human",
    end_on=("human", "tool"),
    include_system=True,
)
```

`max_tokens=6000` 非常激进。`programmer_1` 在循环前做了：`make_plan` → `edit_plan` → `bash` → 4 次 `write_file`（database.py/models.py/schemas.py/seed.py）→ `edit_plan`。每次 `write_file` 的 AIMessage 携带完整文件内容（tool_call args），ToolMessage 返回 diff——**4 次 write_file 产生 8 条消息，轻松超过 6000 token**。

截断后，LLM 看到的上下文变成：
1. `SystemMessage`（含**过时的**计划）
2. `HumanMessage`（任务描述，被 [llm.py#L91-L96](file:///Users/shenweizhang/Desktop/multi_agent_orchestration_v0.0.2/orchestration/workers/nodes/llm.py#L91-L96) 强制保留）
3. 最近 2-3 条消息

**早期的 `make_plan` 调用和结果被截掉了**。LLM 看不到自己已经建过计划、写过文件，只看到"任务 + 系统提示词里说要先规划"。于是它再次调用 `make_plan`。

更严重的是：`trim_messages` 可能**截断 tool_call / ToolMessage 配对的中间**——保留 AIMessage(tool_calls) 但裁掉对应 ToolMessage，或反过来。DeepSeek 对这种断裂的 tool_call 序列非常敏感，容易退化成重复调用同一个工具。

### 根因 2：`prepare` 节点只在开始时注入一次计划，之后永不刷新

[prepare.py#L86-L107](file:///Users/shenweizhang/Desktop/multi_agent_orchestration_v0.0.2/orchestration/workers/nodes/prepare.py#L86-L107) 将 `## CURRENT PLAN` 注入 SystemMessage。但图结构是（[graph.py#L64-L69](file:///Users/shenweizhang/Desktop/multi_agent_orchestration_v0.0.2/orchestration/workers/graph.py#L64-L69)）：

```
START → prepare → llm ↔ tools → summarize → END
```

`prepare` 只执行**一次**。之后 `make_plan` / `edit_plan` 通过 `Command(update={"sub_agent_plan": ...})` 更新了 state 里的 `sub_agent_plan`，但 **SystemMessage 永远不会刷新**。

这意味着在整个 ReAct 循环中，LLM 的系统提示词里看到的计划是 `prepare` 时的快照（最初 phase 1 = in_progress），而不是最新的（phase 3 = in_progress）。LLM 无法通过系统提示词感知计划的当前状态。

### 根因 3：`make_plan` 每次调用都覆盖整个计划

[plan.py 描述](file:///Users/shenweizhang/Desktop/multi_agent_orchestration_v0.0.2/orchestration/tools/description/plan.py#L14) 明确写了：

> "Calling again overwrites the plan."

[programmer.py#L189-L192](file:///Users/shenweizhang/Desktop/multi_agent_orchestration_v0.0.2/orchestration/tools/bundles/programmer.py#L189-L192) 中 `make_plan` 返回 `Command(update={"sub_agent_plan": r["plan"]})`，而 state 的 reducer 是 `lambda _left, right: right`（[state.py#L31](file:///Users/shenweizhang/Desktop/multi_agent_orchestration_v0.0.2/orchestration/workers/state.py#L31)），即**直接覆盖**。

每次重复调用 `make_plan`，所有 phase 状态被重置为 `pending`/`in_progress`，之前的进度全部丢失。这形成了恶性循环。

### 根因 4（放大器）：`MAX_ITERATIONS = 50` 太高

[llm.py#L14](file:///Users/shenweizhang/Desktop/multi_agent_orchestration_v0.0.2/orchestration/workers/nodes/llm.py#L14) 设置上限为 50。虽然最终确实触发了（日志第 341 行），但已经浪费了 50 次 LLM 调用 + 50 次 make_plan 工具调用，约 2 分钟的计算和 token 消耗。

---

## 触发链路

```
1. programmer 调用 make_plan（创建计划，phase 1 in_progress）
2. programmer 写文件 ×4（database.py / models.py / schemas.py / seed.py）
   → 消息历史膨胀，超过 6000 token
3. edit_plan 标记 phase 1,2 done, phase 3 in_progress
   → state 更新了，但 SystemMessage 没刷新
4. trim_messages 截断早期消息
   → make_plan/edit_plan 的上下文丢失，tool_call 配对可能断裂
5. LLM 看到的上下文：SystemMessage(过时计划) + HumanMessage(任务) + 最近几条消息
   → LLM 认为"我还没规划过"，调用 make_plan
6. make_plan 覆盖计划（phase 1 → pending），进度归零
7. 截断后的上下文仍然看不到足够历史 → 再次调用 make_plan
8. 死循环 × 50，直到 iteration_limit 触发
```

---

## 修改方案

### 方案 A（必做，最关键）：修复 `trim_messages` 的 tool_call 配对保护

**文件**：`orchestration/workers/nodes/llm.py`

在 `trim_messages` 之后，增加逻辑确保不出现断裂的 tool_call 序列。两种实现方式选一：

**方式 1（推荐）**：用 `allow_tool_calls=False` 或自定义逻辑，在截断后检查：如果第一条消息是 `ToolMessage`（说明其对应的 AIMessage 被截掉了），则从前面补上对应的 AIMessage，或者干脆丢弃这个孤立的 ToolMessage。

**方式 2**：提高 `max_tokens` 到 12000-16000，减少截断频率。但这只是缓解，不根治。

**方式 3（最佳）**：不用 `trim_messages`，改为手动保留 `[SystemMessage, HumanMessage]` + 最近 N 条**完整的** (AIMessage+ToolMessage) 配对。这样永远不会出现断裂的配对。

### 方案 B（必做）：让计划在每次 LLM 调用时注入最新状态

**文件**：`orchestration/workers/nodes/llm.py`

在 `llm_node` 中，每次调用 LLM 前，从 `state["sub_agent_plan"]` 读取最新计划，动态注入到 SystemMessage 或以 HumanMessage 形式追加。这样 LLM 始终能看到当前计划的实时状态，不会因为系统提示词过时而重新创建计划。

或者更优雅地：将计划注入逻辑从 `prepare` 移到 `llm` 节点，每次循环都刷新。

### 方案 C（推荐）：让 `make_plan` 在已有计划时拒绝重复创建

**文件**：`orchestration/tools/bundles/programmer.py` 或 `orchestration/tools/_kernel/_plan.py`

在 `make_plan` 中增加检查：如果 `runtime.state.get("sub_agent_plan")` 已存在且非空，则返回错误提示 `"Plan already exists. Use edit_plan to update, or delete_plan first to recreate."`，而不是静默覆盖。这直接阻止死循环的触发。

### 方案 D（推荐）：降低 `MAX_ITERATIONS` 并增加重复检测

**文件**：`orchestration/workers/nodes/llm.py`

1. 将 `MAX_ITERATIONS` 从 50 降到 **15-20**
2. 增加重复工具调用检测：如果连续 N 次（如 3 次）调用同一个工具且参数高度相似，直接强制走 `summarize` 节点结束

### 方案 E（可选）：截断 write_file 的大输出

**文件**：`orchestration/tools/_kernel/_fs_mutate.py` 或 `orchestration/tools/bundles/programmer.py`

`write_file` / `str_replace` 的 ToolMessage 返回中包含完整 diff，这会快速撑爆 token 预算。可以截断 diff 输出（只保留前后 N 行），减少消息膨胀速度。

---

## 总结

| 问题 | 影响 | 优先级 |
|------|------|--------|
| `trim_messages` 截断破坏 tool_call 配对 + 丢失历史 | **直接触发死循环** | P0 |
| `prepare` 只注入一次计划，ReAct 循环中不刷新 | LLM 看不到当前计划状态 | P0 |
| `make_plan` 每次覆盖计划 | 恶性循环放大器 | P0 |
| `MAX_ITERATIONS=50` 太高 | 50 次空转才停止 | P1 |

**最小修复**：先做方案 C（make_plan 已有计划时拒绝重建）+ 方案 B（llm 节点动态注入最新计划），这两步就能彻底阻止死循环。方案 A 和 D 作为防御性加固。