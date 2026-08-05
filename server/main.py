"""
Main Entry Point for the Multi-Agent Orchestration API.
"""

import logging
from contextlib import asynccontextmanager

from server.db.session import close_pool, init_pool
from server.router import health, orch
from utils.logging import setup_logging
from utils.settings import setup_langsmith_tracing, settings

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Use the application lifecycle manager to initialize 
    and shut down the global resources.
    """
    setup_logging(
        dev_mode=settings.dev_mode,
        log_level=getattr(logging, settings.log_level, logging.INFO),
    )
    setup_langsmith_tracing()
    await init_pool(settings.database_url)
    yield
    await close_pool()


app = FastAPI(
    title="Multi-Agent Orchestration v0.0.2",
    description="API for Multi-Agent Orchestration v0.0.2",
    version="0.0.2",
    lifespan=lifespan,
)

if settings.dev_mode:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
# else:
#     # Production mode CORS permission has not been set.


app.include_router(health.router, prefix="/api")
app.include_router(orch.router, prefix="/api")
