"""Промпты для саммари, черновика ответа и «где мы остановились»."""

from src.core.chat_service import message_to_text
from src.core.style_profile import style_profile_as_prompt_hint
from src.core.text_sanitizer import sanitize_html
from src.core.timeutil import fmt_local, now_in_tz
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


ANSWER_SYSTEM = (
    "Тобі дають історію переписки/каналу і конкретне питання власника. Дай ПРЯМУ, коротку "
    "відповідь по суті, спираючись лише на цю історію — не переказуй весь чат.\n"
    "Часові мітки повідомлень і поточний час власника (якщо вказаний) — уже в ЙОГО часовому "
    "поясі, конвертувати нічого не треба. Якщо в повідомленні є відносний час («за годину», "
    "«через 30 хв», «завтра»), рахуй його від часу ЦЬОГО конкретного повідомлення і давай "
    "фінальну відповідь теж у цьому ж часі.\n"
    "Якщо відповіді в історії немає — прямо скажи, що не знайшов, не вигадуй.\n"
    "Використовуй HTML-розмітку aiogram (<b>, <i>, <code>). Без markdown."
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


async def answer_question(
    provider: LLMProvider,
    contact: Contact,
    messages: list[Message],
    question: str,
    *,
    tz_name: str | None = None,
    heavy: bool = False,
) -> str:
    # На відміну від message_to_text (UTC) тут мітки часу — в TZ власника: LLM погано рахує
    # відносний час («за годину») без явного часового поясу, а UTC-мітка без підпису читається
    # як локальний час і зсуває відповідь на кілька годин.
    def _line(m: Message) -> str:
        who = "Я" if m.is_outgoing else (m.sender_name or "Вони")
        body = m.transcript or m.text or m.extracted_text or f"[{m.kind}]"
        when = fmt_local(m.date, tz_name) if tz_name else m.date.strftime("%Y-%m-%d %H:%M")
        return f"[{when}] {who}: {body}"

    transcript = "\n".join(_line(m) for m in messages)
    now_line = (
        f"Поточний час власника: {now_in_tz(tz_name).strftime('%Y-%m-%d %H:%M')} ({tz_name}).\n\n"
        if tz_name else ""
    )
    user_prompt = (
        f"Чат/канал: {contact.display_name}\n\n"
        f"{now_line}"
        f"Історія (останні {len(messages)} повідомлень, час — у часовому поясі власника):\n{transcript}\n\n"
        f"Питання власника: {question}"
    )
    raw = await provider.chat(
        [
            ChatMessage(role="system", content=ANSWER_SYSTEM),
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
