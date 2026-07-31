好，让我系统扫描一下。
扫描完毕。下面是我的判断：

---

## 结论：分层清晰，底层稳、上层乱

```
🟢 直接复用（零改动）
├── utils/settings.py         配置管理，pydantic-settings，完美
├── utils/logging.py          structlog 日志，独立
├── utils/model.py            init_model + ainvoke_with_retry + count_tokens，核心基础设施
├── utils/callbacks.py        OrchestrationCallBack 身份追踪链，自洽无 bug
├── utils/common.py           get_workspace()，6 行纯工具
│
├── tools/_kernel/_bash.py        bash 执行 + 安全黑名单
├── tools/_kernel/_fs_readonly.py view_file / glob_tool / grep_tool
├── tools/_kernel/_fs_mutate.py   write_file / str_replace（原子写入+文件锁）
├── tools/_kernel/_plan.py        make_plan / edit_plan / delete_plan（纯验证逻辑）
├── tools/_kernel/_web.py         web_search / fetch_web（DDG+Tavily+crawl4ai）
├── tools/_kernel/_orch_control.py fanout / end 的验证逻辑
├── tools/_kernel/sandbox/         macOS Seatbelt 沙箱
│
├── tools/description/        所有工具的 description 文本，纯常量

🟡 逻辑可复用，但 wrapper 要适配新 state
├── tools/bundles/orchestrator.py   thin wrapper，但用了 ToolRuntime+Command
├── tools/bundles/programmer.py     同上
│
├── utils/summarize.py              压缩逻辑没问题，但在 add_messages 下旧消息删不掉
│                                    → 改用普通 list 后可直接复用

🔴 必须重写
├── orchestration/state.py          OrchestrationState 设计过于复杂
├── orchestration/graph.py          主 graph + routing（orchestrator 不调用 end_orchestration 的 bug）
├── orchestration/orchestrator.py   orchestrator_node 实现
├── orchestration/workers/state.py  SubAgentState ← add_messages 死循环根因
├── orchestration/workers/graph.py  子代理 ReAct graph
├── orchestration/workers/nodes/     llm / prepare / summarize 节点
├── orchestration/prompts/          所有 system prompt（约束效果差）
```

---

### 核心判断：`tools/_kernel/` 和 `utils/` 是项目最值钱的部分

- **`_kernel/` 下的 6 个工具文件 + sandbox**：总计约 2000 行生产级代码，安全沙箱、SSRF 防护、原子写入、双向配对保护——这些跟编排逻辑零耦合，换了上层框架照样用。

- **`utils/model.py`** 的 `ainvoke_with_retry`：3 次指数退避 + RateLimitError 特殊处理 + 7 种可重试异常，被整个项目调用。

- **`utils/callbacks.py`** 的三级身份解析链（tool_call_id → structlog contextvars → parent_run_id）：解决了 LangGraph 并发子代理下工具调用的身份归属问题，非常精巧。

这些不需要动。你重构的重点是 `orchestration/` 目录下的编排逻辑和 `workers/state.py` 里那个 `add_messages` 导致的死循环。