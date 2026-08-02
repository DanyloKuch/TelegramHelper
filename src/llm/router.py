from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import User
from src.db.repo import get_api_key
from src.llm.base import LLMProvider


async def build_provider(session: AsyncSession, user: User) -> LLMProvider | None:
    """Создаёт провайдера согласно настройкам пользователя. None — если ключ не задан.

    Классы провайдеров импортируются здесь, а не сверху: иначе в память тянутся
    ОБА SDK (openai + google-genai ≈ 120 МБ), хотя работает всегда только один.
    """
    provider_name = user.settings.llm_provider if user.settings else "openai"
    key = await get_api_key(session, user, provider_name)
    if not key:
        return None
    if provider_name == "openai":
        from src.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(key)
    if provider_name == "gemini":
        from src.llm.gemini_provider import GeminiProvider

        return GeminiProvider(key)
    return None
