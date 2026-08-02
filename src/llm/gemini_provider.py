import asyncio

from src.config import LLMDefaults
from src.llm.base import ChatMessage, with_output_language


def _to_gemini_contents(messages: list[ChatMessage]) -> tuple[str | None, list[dict]]:
    """Возвращает (system_instruction, contents) для google-genai."""
    system_chunks: list[str] = []
    contents: list[dict] = []
    for m in messages:
        if m.role == "system":
            system_chunks.append(m.content)
        else:
            role = "model" if m.role == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": m.content}]})
    system = "\n\n".join(system_chunks) if system_chunks else None
    return system, contents


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str) -> None:
        # Ленивый импорт: google-genai стоит ~80 МБ RSS и нужен только своему провайдеру.
        from google import genai

        self._client = genai.Client(api_key=api_key)

    async def validate_key(self) -> bool:
        def _check() -> bool:
            try:
                # пагинированный итератор; первый элемент достаточен
                next(iter(self._client.models.list()))
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_check)

    async def chat(self, messages: list[ChatMessage], *, heavy: bool = False) -> str:
        model = LLMDefaults.GEMINI_CHAT_HEAVY if heavy else LLMDefaults.GEMINI_CHAT_LIGHT
        system, contents = _to_gemini_contents(with_output_language(messages))

        def _call() -> str:
            config = {"system_instruction": system} if system else None
            resp = self._client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            return resp.text or ""

        return await asyncio.to_thread(_call)

    async def embed(self, text: str) -> list[float]:
        def _call() -> list[float]:
            resp = self._client.models.embed_content(
                model=LLMDefaults.GEMINI_EMBED,
                contents=text,
            )
            return list(resp.embeddings[0].values)

        return await asyncio.to_thread(_call)
