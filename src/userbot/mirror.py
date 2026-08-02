"""Зеркало всех сообщений (входящих и исходящих) в БД и FTS5 в реальном времени.
Транскрипция голоса и парсинг документов — лениво в момент анализа."""
from __future__ import annotations

import logging
from datetime import datetime

from telethon import TelegramClient, events
from telethon.tl.custom import Message as TgMessage
from telethon.tl.types import User as TgUser

from src.config import settings
from src.db.repo import get_or_create_user, upsert_message, upsert_contact
from src.db.session import get_session


logger = logging.getLogger(__name__)


def _classify(msg: TgMessage) -> str:
    if msg.voice:
        return "voice"
    if msg.audio:
        return "audio"
    if msg.document:
        return "document"
    if msg.photo:
        return "photo"
    if msg.text:
        return "text"
    return "other"


def _peer_id_of(msg: TgMessage) -> int | None:
    chat = msg.chat
    if chat is not None:
        return chat.id
    if msg.peer_id is not None and hasattr(msg.peer_id, "user_id"):
        return msg.peer_id.user_id
    return msg.chat_id


async def _sender_label(msg: TgMessage) -> str | None:
    if msg.out:
        return None  # это мы сами
    try:
        sender = await msg.get_sender()
    except Exception:
        sender = None
    if sender is None:
        return None
    parts = [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]
    name = " ".join(p for p in parts if p).strip()
    if name:
        return name
    return getattr(sender, "username", None) or str(sender.id)


def attach_mirror(client: TelegramClient, owner_telegram_id: int) -> None:
    async def on_message(event: events.NewMessage.Event) -> None:
        try:
            msg: TgMessage = event.message
            peer_id = _peer_id_of(msg)
            if not peer_id:
                return
            if peer_id == settings.control_bot_id:
                # Переписка владельца с самим control-ботом — служебная, не контент
                # для саммари/поиска. Без этого фильтра /search находит ответы бота.
                return

            kind = _classify(msg)
            text = msg.text or msg.message or None
            sender_name = await _sender_label(msg)

            async with get_session() as session:
                owner = await get_or_create_user(session, owner_telegram_id)

                try:
                    chat = await event.get_chat()
                except Exception:
                    chat = None
                if chat is not None:
                    if isinstance(chat, TgUser):
                        parts = [getattr(chat, "first_name", None), getattr(chat, "last_name", None)]
                        display = " ".join(p for p in parts if p).strip() or (chat.username or str(peer_id))
                        await upsert_contact(
                            session, owner,
                            peer_id=peer_id, peer_kind="user",
                            is_bot=bool(getattr(chat, "bot", False)),
                            display_name=display,
                            username=getattr(chat, "username", None),
                            phone=getattr(chat, "phone", None),
                        )
                    else:
                        title = getattr(chat, "title", None) or str(peer_id)
                        kind_chat = "channel" if getattr(chat, "broadcast", False) else "chat"
                        await upsert_contact(
                            session, owner,
                            peer_id=peer_id, peer_kind=kind_chat,
                            is_bot=False,
                            display_name=title,
                            username=getattr(chat, "username", None),
                        )

                await upsert_message(
                    session,
                    user_id=owner.id,
                    peer_id=peer_id,
                    message_id=msg.id,
                    sender_id=msg.sender_id,
                    sender_name=sender_name,
                    is_outgoing=bool(msg.out),
                    date=msg.date.replace(tzinfo=None) if msg.date else datetime.utcnow(),
                    kind=kind,
                    text=text,
                    transcript=None,
                    media_path=None,
                    extracted_text=None,
                )
        except Exception:
            logger.exception("mirror handler failed")

    client.add_event_handler(on_message, events.NewMessage())
    logger.info("Mirror handler attached for user %s", owner_telegram_id)
