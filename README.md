## Multi-Agent Orchestration v0.0.2

A LangGraph-based multi-agent orchestration system that coordinates an orchestrator with sub-agents to tackle tasks ranging from easy to complex.

### Features

- Orchestrator-driven planning with parallel worker fanout
- Built-in tool ecosystem (filesystem, shell, web search)
- Context engineering with auto-compaction
- Streaming responses via SSE
- FastAPI backend with PostgreSQL persistence

### Quick Start

```bash
docker compose up -d

source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   
# configure your model & DB settings in .env

chmod +x clear_cache.sh
chmod +x run_app.sh

./clear_cache.sh
./run_app.sh
```

### Structure

- `orchestration/` — core orchestration graph, workers, prompts, tools
- `server/` — FastAPI app, routers, schemas
- `utils/` — shared settings, logging, callbacks
- `tests/` — test suite
