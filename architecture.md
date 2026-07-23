# Orchestration Architecture v0.0.2

```mermaid
graph TD
    Start([START]) --> Orchestrator

    Orchestrator["orchestrator<br/>(llm_node)"]

    Orchestrator -->|"should_stop"| End([END])
    Orchestrator -->|"should_pause"| Interrupt["interrupt<br/>human gate"]
    Orchestrator -->|"!should_pause && tool_calls"| Tools["tools_node"]
    Orchestrator -->|"!should_pause && no_tool_calls"| RouteNext{"route_next"}

    Tools -->|result| Orchestrator

    RouteNext -->|"有 response"| End
    RouteNext -->|"有 round_tasks"| Fanout

    subgraph Fanout["fanout"]
        Send["Send<br/>N workers"]
        Workers["workers<br/>writer · reviewer · search · ..."]
        Send --> Workers
    end

    Workers -->|结果汇总| Orchestrator

    Interrupt -->|"沟通后继续"| Orchestrator
```
