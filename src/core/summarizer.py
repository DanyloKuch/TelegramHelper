"""Промпты для саммари, черновика ответа и «где мы остановились»."""

from src.core.chat_service import message_to_text
from src.core.style_profile import style_profile_as_prompt_hint
from src.core.text_sanitizer import sanitize_html
from src.db.models import Contact, Message
from src.llm.base import ChatMessage, LLMProvider


SUMMARY_SYSTEM = (
    "Ти робиш стисле самарі листування. Структура відповіді:\n"
    "📝 <b>Головне</b> — 2–4 булети.\n"
    "🎯 <b>Відкриті питання / задачі</b> — чого від мене чекають.\n"
    "📅 <b>Домовленості</b> — дати, зустрічі, обіцянки (з датою, якщо є).\n"
    "🌡 <b>Тон</b> — однією фразою.\n"
    "Використовуй HTML-розмітку aiogram (<b>, <i>, <code>). Без markdown."
)


DRAFT_SYSTEM = (
    "Ти пишеш чернетку відповіді від мого імені. Лише текст відповіді, без префіксів і пояснень.\n"
    "Враховуй контекст останніх повідомлень і не повторюй уже сказане.\n"
    "Якщо важлива інформація неоднозначна — постав коротке уточнювальне питання замість домислу."
)


CATCHUP_SYSTEM = (
    "Я давно не відповідав у цьому чаті. Зроби:\n"
    "1) <b>На чому зупинились</b> — 2–3 булети про поточний стан.\n"
    "2) <b>Чого від мене чекають</b> — що потрібно відповісти або зробити.\n"
    "3) <b>Чернетка відповіді</b> — 1–4 речення, у моєму стилі.\n"
    "Використовуй HTML-розмітку aiogram."
)


async def summarize_chat(
    provider: LLMProvider,
    contact: Contact,
    messages: list[Message],
    *,
    heavy: bool = False,
) -> str:
    transcript = "\n".join(message_to_text(m) for m in messages)
    user_prompt = (
        f"Співрозмовник: {contact.display_name}\n\n"
        f"Листування (останні {len(messages)} повідомлень):\n{transcript}"
    )
    raw = await provider.chat(
        [
            ChatMessage(role="system", content=SUMMARY_SYSTEM),
            ChatMessage(role="user", content=user_prompt),
        ],
        heavy=heavy,
    )
    return sanitize_html(raw)


async def draft_reply(
    provider: LLMProvider,
    contact: Contact,
    messages: list[Message],
    *,
    instruction: str | None = None,
    heavy: bool = False,
) -> str:
    transcript = "\n".join(message_to_text(m) for m in messages)
    style_hint = style_profile_as_prompt_hint(contact.style_profile)
    system = DRAFT_SYSTEM
    if style_hint:
        system = system + "\n" + style_hint
    user_prompt = (
        f"Співрозмовник: {contact.display_name}\n\n"
        f"Контекст листування:\n{transcript}\n\n"
        + (f"Інструкція: {instruction}" if instruction else "Напиши доречну відповідь на останнє повідомлення.")
    )
    raw = await provider.chat(
        [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=user_prompt),
        ],
        heavy=heavy,
    )
    return sanitize_html(raw)


async def catchup(
    provider: LLMProvider,
    contact: Contact,
    messages: list[Message],
    *,
    heavy: bool = False,
) -> str:
    transcript = "\n".join(message_to_text(m) for m in messages)
    style_hint = style_profile_as_prompt_hint(contact.style_profile)
    system = CATCHUP_SYSTEM
    if style_hint:
        system = system + "\n" + style_hint
    user_prompt = (
        f"Співрозмовник: {contact.display_name}\n\nОстанні повідомлення:\n{transcript}"
    )
    raw = await provider.chat(
        [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=user_prompt),
        ],
        heavy=heavy,
    )
    return sanitize_html(raw)
