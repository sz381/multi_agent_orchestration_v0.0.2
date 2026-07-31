你说得对，让我查一下是不是 LangGraph 的已知行为。
我翻了半天代码和日志，找到几个关键线索：

---

## 线索 1：`summarize.py` L184 写入了 `"messages"` 而不是 `"sub_agent_messages"`
每个子代理完成时，它的输出 AIMessage 被写入 `"messages"`（这是父图 OrchestrationState 的 key，不是子图 SubAgentState 的 `"sub_agent_messages"`）。LangGraph 做状态合并时，这个 AIMessage 就会通过 `add_messages` reducer 追到 orchestrator 的 `messages` 里。

这就是为什么日志里 `total_messages` 在子代理执行期间不断上涨（17→19→21→23...）。

---

## 线索 2：日志时间线揭示的诡异模式

```
23:59:00  子代理 A+B 开始运行
23:59:21  conversation_summarize_triggered → LLM Start orchestrator (run_id=ef14)
23:59:25  conversation_summarize_triggered → LLM Start orchestrator (run_id=703a) ← 第二个！
23:59:25  LLM End has_tool_calls=False (ef14)
23:59:27  conversation_summarize_triggered → LLM Start orchestrator (run_id=2dc8) ← 第三个！
23:59:29  LLM End has_tool_calls=False (703a)
23:59:32  LLM End has_tool_calls=False (2dc8)
23:59:34  conversation_summarize_triggered → LLM Start orchestrator (run_id=cb7d0) ← 第四个！
```

**11 秒内 3 个 orchestrator LLM 调用同时重叠执行**，全部是 `has_tool_calls=False`（summary model）。

---

## 我的判断

老实说，从代码层面我没找到一个确定的"因为 X 所以 Y"的机制来解释为什么 orchestrator 在子代理运行期间被反复触发。counter 逻辑本身是对的，图拓扑也没有到 orchestrator 的其他路径。

但我怀疑这可能是 **LangGraph 的 Send API + subgraph + Command.update 三者交互的边界行为**。具体来说：

1. 子代理 subgraph 作为父图节点，完成后状态合并到父图
2. 子代理的 `"messages"` key（L184）和父图的 `"messages"` key 同名 → 子代理输出被合并进 orchestrator 的消息列表
3. 在某版本 LangGraph 中，状态合并可能触发了父图的某种"重新评估"，导致 orchestrator 被错误调度

**但这只是猜测**。要不要我在 orchestrator 节点入口加一行 `logger.info("orchestrator_entered", counter=state.get("active_sub_agent_count"))` 日志，下次跑的时候就能精准确认 orchestrator 是从哪个路径被唤醒的？这样不用猜。