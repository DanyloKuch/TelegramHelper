"""Профиль стиля общения с конкретным контактом. Подмешивается в промпт авто-ответа
и черновика, чтобы они звучали так же, как обычно пишет владелец этому собеседнику."""
import json
import logging
from datetime import datetime

from src.core.chat_service import message_to_text
from src.db.models import Contact, User
from src.db.repo import (
    fetch_my_messages_in_chat,
    get_contact,
    get_or_create_user,
)
from src.db.session import get_session
from src.llm.base import ChatMessage, LLMProvider


logger = logging.getLogger(__name__)


STYLE_SYSTEM = (
    "Ти експерт з аналізу стилю листування. За набором повідомлень ОДНОГО автора "
    "поверни компактний JSON-профіль його стилю спілкування з конкретним співрозмовником.\n\n"
    "Поля JSON:\n"
    '  "address": як звертається ("ти"/"ви"/ім\'я/ніяк),\n'
    '  "register": формальний | розмовний | дружній | офіційний,\n'
    '  "length": короткі | середні | розгорнуті,\n'
    '  "emoji_usage": none | rare | moderate | frequent,\n'
    '  "punctuation": строга | розслаблена (точки в кінці, оклики),\n'
    '  "typical_openings": [до 3 типових вітань або зачинів],\n'
    '  "typical_closings": [до 3 типових завершень],\n'
    '  "phrases": [до 5 характерних фраз/слів-маркерів],\n'
    '  "notes": одна-дві фрази — загальне відчуття від стилю.\n\n'
    "Повертай ЛИШЕ валідний JSON, без префіксів і markdown."
)


def _parse_json_safe(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        logger.warning("Style profile JSON parse failed, using empty: %r", text[:120])
        return {}


async def build_style_profile(
    provider: LLMProvider,
    *,
    contact_label: str,
    my_messages_text: str,
) -> dict:
    if not my_messages_text.strip():
        return {}
    user_prompt = (
        f"Собеседник: {contact_label}.\n"
        "Ниже — мои (автора) сообщения этому собеседнику:\n\n"
        f"{my_messages_text}\n\n"
        "Сформируй JSON-профиль моего стиля общения с этим собеседником."
    )
    raw = await provider.chat(
        [
            ChatMessage(role="system", content=STYLE_SYSTEM),
            ChatMessage(role="user", content=user_prompt),
        ],
        heavy=False,
    )
    return _parse_json_safe(raw)


async def update_style_profile_for_contact(
    provider: LLMProvider,
    owner_telegram_id: int,
    peer_id: int,
    *,
    sample_size: int = 80,
) -> dict | None:
    async with get_session() as session:
        owner: User = await get_or_create_user(session, owner_telegram_id)
        my_msgs = await fetch_my_messages_in_chat(session, owner, peer_id, limit=sample_size)
        contact: Contact | None = await get_contact(session, owner, peer_id)

    if not my_msgs or contact is None:
        return None

    text = "\n".join(message_to_text(m) for m in my_msgs)
    profile = await build_style_profile(
        provider,
        contact_label=contact.display_name,
        my_messages_text=text,
    )
    if not profile:
        return None

    async with get_session() as session:
        owner = await get_or_create_user(session, owner_telegram_id)
        contact = await get_contact(session, owner, peer_id)
        if contact is not None:
            contact.style_profile = json.dumps(profile, ensure_ascii=False)
            contact.style_updated_at = datetime.utcnow()

    return profile


def style_profile_as_prompt_hint(profile_json: str | None) -> str:
    if not profile_json:
        return ""
    try:
        p = json.loads(profile_json)
    except Exception:
        return ""

    parts: list[str] = []
    if p.get("address"):
        parts.append(f"обращение: {p['address']}")
    if p.get("register"):
        parts.append(f"регистр: {p['register']}")
    if p.get("length"):
        parts.append(f"длина: {p['length']}")
    if p.get("emoji_usage"):
        parts.append(f"эмодзи: {p['emoji_usage']}")
    if p.get("punctuation"):
        parts.append(f"пунктуация: {p['punctuation']}")
    if p.get("typical_openings"):
        parts.append("типичные зачины: " + ", ".join(p["typical_openings"]))
    if p.get("phrases"):
        parts.append("маркерные фразы: " + ", ".join(p["phrases"]))
    if p.get("notes"):
        parts.append("заметки: " + p["notes"])

    return "Пиши в моём стиле общения с этим собеседником: " + "; ".join(parts) + "."
