import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).parent.parent / ".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )
    
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model_name: str = "deepseek-v4-flash"
    
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


def setup_langsmith_tracing():
    global _langsmith_initialized
    if _langsmith_initialized:
        return

    if settings.langsmith_api_key and settings.langsmith_tracing:
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
        os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
        os.environ["LANGSMITH_TRACING"] = "true"
        _get_logger().info(f"LangSmith automatic tracking is Enabled, project={settings.langsmith_project}")
    else:
        _get_logger().info("LangSmith automatic tracking is Disabled")

    _langsmith_initialized = True
