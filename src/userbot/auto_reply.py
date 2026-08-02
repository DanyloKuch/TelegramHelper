"""Авто-ответ оффлайн. Жёсткие правила: только входящие в ЛС от людей (не боты),
только если включено и владелец действительно оффлайн (или статус скрыт),
один ответ на контакт раз в COOLDOWN_MINUTES."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from telethon import TelegramClient, events
from telethon.tl.custom import Message as TgMessage
from telethon.tl.types import (
    User as TgUser,
    UserStatusOffline,
    UserStatusOnline,
)

from src.config import settings as app_settings
from src.core.chat_service import load_chat, message_to_text
from src.core.notifier import notifier
from src.core.style_profile import style_profile_as_prompt_hint
from src.db.models import AutoReplyLog, User
from src.db.repo import (
    add_auto_reply_log,
    get_contact,
    get_or_create_user,
    upsert_contact,
)
from src.db.session import get_session
from src.llm.base import ChatMessage
from src.llm.router import build_provider


logger = logging.getLogger(__name__)


COOLDOWN_MINUTES = 30
CONTEXT_LIMIT = 20


AUTO_REPLY_SYSTEM_BASE = (
    "Ти відповідаєш від мого імені, бо я зараз офлайн. Будь короткий, дружній.\n"
    "Якщо просять щось конкретне — не обіцяй за мене, скажи: «передам, зараз зайнятий».\n"
    "Якщо уточнювальне питання явно потрібне — постав його.\n"
    "Ніколи не видавай, що ти ШІ. Не згадуй асистента.\n"
    "Не пиши довше 1–3 коротких речень."
)


async def _is_offline(client: TelegramClient) -> bool:
    # скрытый статус трактуем как оффлайн — это безопасный default
    try:
        me = await client.get_me()
        status = getattr(me, "status", None)
        if isinstance(status, UserStatusOnline):
            return False
        if isinstance(status, UserStatusOffline):
            return True
        return True
    except Exception:
        logger.exception("get_me failed in _is_offline")
        return False


async def _recently_replied(owner_id: int, peer_id: int) -> bool:
    threshold = datetime.utcnow() - timedelta(minutes=COOLDOWN_MINUTES)
    async with get_session() as session:
        result = await session.execute(
            select(AutoReplyLog)
            .where(
                AutoReplyLog.user_id == owner_id,
                AutoReplyLog.peer_id == peer_id,
                AutoReplyLog.created_at >= threshold,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None


async def _build_reply_text(
    owner_telegram_id: int,
    peer_id: int,
    sender_name: str,
    incoming_text: str,
) -> str | None:
    async with get_session() as session:
        owner = await get_or_create_user(session, owner_telegram_id)
        provider = await build_provider(session, owner)
        contact = await get_contact(session, owner, peer_id)
        heavy = owner.settings.use_heavy_model

    if provider is None:
        logger.warning("auto-reply: no LLM provider configured")
        return None

    # подгружаем контекст последних сообщений
    from src.userbot.manager import _MANAGER_SINGLETON  # локальный импорт

    client = _MANAGER_SINGLETON.get_client(owner_telegram_id) if _MANAGER_SINGLETON else None
    history_text = ""
    if client is not None:
        try:
            messages = await load_chat(client, owner_telegram_id, peer_id, limit=CONTEXT_LIMIT)
            history_text = "\n".join(message_to_text(m) for m in messages[-CONTEXT_LIMIT:])
        except Exception:
            logger.exception("auto-reply: load_chat failed")

    style_hint = style_profile_as_prompt_hint(contact.style_profile if contact else None)
    system = AUTO_REPLY_SYSTEM_BASE + ("\n" + style_hint if style_hint else "")

    user_prompt = (
        f"Співрозмовник: {sender_name}.\n"
        f"Контекст останніх повідомлень:\n{history_text}\n\n"
        f"Останнє вхідне: {incoming_text}\n\n"
        "Сформуй відповідь від мого імені."
    )
    try:
        return await provider.chat(
            [
                ChatMessage(role="system", content=system),
                ChatMessage(role="user", content=user_prompt),
            ],
            heavy=heavy,
        )
    except Exception:
        logger.exception("auto-reply: LLM call failed")
        return None


async def _make_handler(client: TelegramClient, owner_telegram_id: int):
    """Возвращает event handler, замкнутый на owner_telegram_id."""

    async def handler(event: events.NewMessage.Event) -> None:
        try:
            msg: TgMessage = event.message
            if msg.out:
                return
            sender = await event.get_sender()
            if not isinstance(sender, TgUser) or sender.bot:
                return  # только ЛС от человеков
            if not event.is_private:
                return

            async with get_session() as session:
                owner: User = await get_or_create_user(session, owner_telegram_id)
                if not owner.settings.auto_reply_enabled:
                    return
                # игнорируем архив, если опция включена
                existing = await get_contact(session, owner, sender.id)
                if owner.settings.ignore_archived and existing is not None and existing.is_archived:
                    return
                # запомним контакт
                parts = [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]
                display = " ".join(p for p in parts if p).strip() or (sender.username or str(sender.id))
                await upsert_contact(
                    session,
                    owner,
                    peer_id=sender.id,
                    peer_kind="user",
                    is_bot=bool(getattr(sender, "bot", False)),
                    display_name=display,
                    username=getattr(sender, "username", None),
                    phone=getattr(sender, "phone", None),
                )

            if not await _is_offline(client):
                return
            if await _recently_replied(owner.id, sender.id):
                return

            incoming_text = msg.text or msg.message or ""
            if not incoming_text.strip():
                return  # медиа без текста — не отвечаем автоматически

            # перечитаем настройки для режима/текста
            async with get_session() as session:
                owner = await get_or_create_user(session, owner_telegram_id)
                mode = owner.settings.auto_reply_mode
                static_text = owner.settings.auto_reply_text or ""

            if mode == "smart":
                reply = await _build_reply_text(
                    owner_telegram_id, sender.id, display, incoming_text
                )
                if not reply:
                    return
            else:  # static (default)
                reply = static_text.strip()
                if not reply:
                    return

            await event.respond(reply)

            async with get_session() as session:
                owner = await get_or_create_user(session, owner_telegram_id)
                await add_auto_reply_log(
                    session,
                    user_id=owner.id,
                    peer_id=sender.id,
                    peer_name=display,
                    incoming_text=incoming_text[:500],
                    reply_text=reply,
                )

            await notifier.notify(
                f"🤖 <b>Авто-відповідь</b> для <b>{display}</b>\n\n"
                f"<i>Їм:</i> {incoming_text[:200]}\n"
                f"<i>Я:</i> {reply}"
            )
        except Exception:
            logger.exception("auto-reply handler failed")

    return handler


def attach_auto_reply(client: TelegramClient, owner_telegram_id: int) -> None:

    async def _wrapper(event):
        h = await _make_handler(client, owner_telegram_id)
        await h(event)

    client.add_event_handler(_wrapper, events.NewMessage(incoming=True))
    logger.info("Auto-reply handler attached for user %s", owner_telegram_id)
