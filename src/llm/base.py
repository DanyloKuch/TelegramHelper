from dataclasses import dataclass
from typing import Literal, Protocol


Role = Literal["system", "user", "assistant"]


@dataclass
class ChatMessage:
    role: Role
    content: str


# Язык вывода задаётся здесь, а не в каждом промпте по отдельности: провайдеры
# дописывают это правило к системному сообщению, поэтому ни один промпт не может
# «забыть» язык. Формат (JSON и его ключи) правило намеренно не трогает.
OUTPUT_LANGUAGE_RULE = (
    "\n\nМОВА ВІДПОВІДІ: усі тексти, призначені для користувача (відповіді, самарі, "
    "чернетки, формулювання задач і обіцянок), пиши УКРАЇНСЬКОЮ мовою. "
    "Структуру відповіді не змінюй: якщо очікується JSON — лишай ті самі ключі "
    "та їхні службові значення англійською, українською пиши лише вміст текстових полів."
)


def with_output_language(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Дописывает правило языка к первому system-сообщению (или добавляет его)."""
    for i, m in enumerate(messages):
        if m.role == "system":
            patched = list(messages)
            patched[i] = ChatMessage(role="system", content=m.content + OUTPUT_LANGUAGE_RULE)
            return patched
    return [ChatMessage(role="system", content=OUTPUT_LANGUAGE_RULE.strip()), *messages]


class LLMProvider(Protocol):
    name: str

    async def validate_key(self) -> bool:
        """Лёгкий запрос: подходит ли ключ. Используется в /settings."""

    async def chat(self, messages: list[ChatMessage], *, heavy: bool = False) -> str:
        ...

    async def embed(self, text: str) -> list[float]:
        ...
