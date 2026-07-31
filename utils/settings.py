"""
Application configuration sourced from ``.env`` via pydantic-settings.

Exposes the ``settings`` singleton and ``setup_langsmith_tracing()`` for
one-shot LangSmith wiring at startup.
"""

import logging
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

import structlog

_logger = structlog.get_logger(__name__)


class Settings(BaseSettings):
    """
    Global application settings.

    All fields have sensible defaults and can be overridden in ``.env``.
    Extra keys are forbidden to catch typos early.
    """

    model_config = SettingsConfigDict(
        env_file=Path(__file__).parent.parent / ".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model_name: str = "deepseek-v4-flash"

    xiaomi_mimo_api_key: str | None = None
    xiaomi_mimo_base_url: str = "https://api.xiaomimimo.com/v1"
    xiaomi_mimo_model_name: str = "mimo-v2.5"

    tavily_api_key: str | None = None

    workspace_dir: str | None = None

    langsmith_tracing: bool = False
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_api_key: str | None = None
    langsmith_project: str = "Mr Orchestra"

    log_level: str = "INFO"
    dev_mode: bool = True


settings = Settings()

_langsmith_initialized = False


def setup_langsmith_tracing() -> None:
    """
    Inject LangSmith settings into environment variables.

    Idempotent — subsequent calls are no-ops. Only activates when both
    ``langsmith_tracing`` and ``langsmith_api_key`` are configured.
    """
    
    global _langsmith_initialized

    if _langsmith_initialized:
        return

    # TODO: setup_langsmith_tracing 直接改 os.environ、缺少运行时配置语义校验。
    if settings.langsmith_api_key and settings.langsmith_tracing:
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
        os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
        os.environ["LANGSMITH_TRACING"] = "true"
        _logger.info("LangSmith automatic tracking is Enabled", project=settings.langsmith_project)
    else:
        _logger.info("LangSmith automatic tracking is Disabled")

    _langsmith_initialized = True
    