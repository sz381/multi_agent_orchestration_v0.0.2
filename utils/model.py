from langchain_openai import ChatOpenAI

from utils.settings import settings


def init_model(
    model_name: str = settings.deepseek_model_name,
    temperature: float = 0.3,
    max_tokens: int = 16384,
    streaming: bool = True,
) -> ChatOpenAI:
    return ChatOpenAI(
        model=model_name,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        streaming=streaming,
    )
