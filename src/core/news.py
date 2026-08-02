"""Дайджест из подписанных каналов на тему: cosine-фильтр постов через embeddings + LLM-сводка."""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient

from src.config import settings as app_settings
from src.core.notifier import notifier
from src.core.text_sanitizer import sanitize_html
from src.core.timeutil import now_in_tz
from src.db.models import Contact, User
from src.db.repo import get_or_create_user, list_contacts, list_news_topics
from src.db.session import get_session
from src.llm.base import ChatMessage, LLMProvider


logger = logging.getLogger(__name__)


NEWS_SYSTEM = (
    "Ти збираєш новинний дайджест за запитаною темою на основі постів із Telegram-каналів.\n"
    "Структура відповіді (HTML aiogram):\n"
    "📰 <b>Тема:</b> ...\n"
    "🔑 <b>Головне</b> — 3–6 булетів, по кожному в дужках вказуй канал.\n"
    "📅 <b>Хронологія</b> — події за часом, якщо важливо.\n"
    "🔀 <b>Розбіжності</b> — де джерела не сходяться (якщо є).\n"
    "Не вигадуй факти, опирайся лише на надіслані пости. Кожен булет — фактологічний."
)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


async def _gather_posts(
    client: TelegramClient,
    channels: list[Contact],
    *,
    hours: int,
    per_channel_limit: int,
) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    posts: list[dict] = []
    for ch in channels:
        try:
            entity = await client.get_entity(ch.peer_id)
        except Exception:
            logger.warning("get_entity failed for channel %s", ch.peer_id)
            continue
        try:
            async for msg in client.iter_messages(entity, limit=per_channel_limit):
                if msg.date and msg.date < cutoff:
                    break
                text = msg.text or msg.message
                if not text or len(text) < 30:
                    continue
                posts.append({
                    "channel_name": ch.display_name,
                    "channel_username": ch.username,
                    "channel_peer_id": ch.peer_id,
                    "message_id": msg.id,
                    "date": msg.date,
                    "text": text,
                })
        except Exception:
            logger.exception("iter_messages failed for channel %s", ch.display_name)
            continue
    return posts


async def build_news_digest(
    client: TelegramClient,
    owner_telegram_id: int,
    topic: str,
    *,
    hours: int = 24,
    per_channel_limit: int = 80,
    top_k: int = 12,
    only_marked_sources: bool = True,
    provider_override: LLMProvider | None = None,
) -> str:
    """Готовит дайджест. Если only_marked_sources=False — берёт все подписанные каналы."""
    async with get_session() as session:
        owner: User = await get_or_create_user(session, owner_telegram_id)
        channels = await list_contacts(
            session, owner, kinds=("channel",),
            include_bots=False,
            only_news_sources=only_marked_sources,
        )
        # если "только помеченные" не дали ничего — fallback на все каналы
        if only_marked_sources and not channels:
            channels = await list_contacts(session, owner, kinds=("channel",))

        provider = provider_override
        if provider is None:
            from src.llm.router import build_provider
            provider = await build_provider(session, owner)
        heavy = owner.settings.use_heavy_model

    if provider is None:
        return "Не заданий LLM-ключ. Налаштуй у /settings → LLM."
    if not channels:
        return "Не знайшов каналів. Спершу /sync, потім познач потрібні через /news_channels."

    posts = await _gather_posts(client, channels, hours=hours, per_channel_limit=per_channel_limit)
    if not posts:
        return f"За останні {hours}год у твоїх каналах постів не знайшов."

    # embedding темы
    try:
        topic_vec = await provider.embed(topic)
    except Exception:
        logger.exception("embed topic failed")
        topic_vec = None

    if topic_vec is not None:
        scored: list[tuple[float, dict]] = []
        for p in posts:
            try:
                v = await provider.embed(p["text"][:1500])
                scored.append((_cosine(topic_vec, v), p))
            except Exception:
                continue
        scored.sort(key=lambda x: x[0], reverse=True)
        # отсекаем шум: оставляем хотя бы топ-N с положительным сходством
        relevant = [p for s, p in scored if s > 0.15][:top_k]
        if not relevant:
            relevant = [p for _, p in scored[:top_k]]
    else:
        # без embeddings — простая фильтрация по вхождению ключевых слов
        kw = topic.lower().split()
        relevant = [
            p for p in posts
            if any(k in p["text"].lower() for k in kw)
        ][:top_k] or posts[:top_k]

    # формируем выжимку для LLM
    lines = []
    for p in relevant:
        when = p["date"].strftime("%Y-%m-%d %H:%M") if p["date"] else "?"
        link_tail = f" (https://t.me/{p['channel_username']}/{p['message_id']})" if p["channel_username"] else ""
        lines.append(f"[{when}] <{p['channel_name']}>{link_tail}\n{p['text'][:1200]}")
    body = "\n\n---\n\n".join(lines)

    user_prompt = (
        f"Тема запиту: {topic}\n"
        f"Вікно: останні {hours} годин\n"
        f"Каналів: {len(channels)}, релевантних постів: {len(relevant)}\n\n"
        f"Пости:\n\n{body}"
    )
    raw = await provider.chat(
        [
            ChatMessage(role="system", content=NEWS_SYSTEM),
            ChatMessage(role="user", content=user_prompt),
        ],
        heavy=heavy,
    )
    return sanitize_html(raw)


async def news_scheduler_loop() -> None:
    last_sent: dict[int, str] = {}
    while True:
        try:
            owner_id = app_settings.owner_telegram_id
            topics_to_run: list[tuple[str, int]] = []
            async with get_session() as session:
                owner = await get_or_create_user(session, owner_id)
                tz_name = owner.settings.timezone
                target_hm = owner.settings.news_digest_time
                enabled = owner.settings.news_enabled
                if enabled:
                    local_now = now_in_tz(tz_name)
                    current_hm = local_now.strftime("%H:%M")
                    current_day = local_now.strftime("%Y-%m-%d")
                    if target_hm == current_hm and last_sent.get(owner_id) != current_day:
                        topics = await list_news_topics(session, owner, only_enabled=True)
                        topics_to_run = [(t.topic, t.hours) for t in topics]
                        last_sent[owner_id] = current_day  # помечаем даже если тем нет

            if topics_to_run:
                from src.userbot.manager import _MANAGER_SINGLETON
                client = _MANAGER_SINGLETON.get_client(owner_id) if _MANAGER_SINGLETON else None
                if client is None:
                    logger.warning("news scheduler: no userbot client for owner %s", owner_id)
                else:
                    await notifier.notify(
                        f"📰 <b>Авто-новини</b> · {len(topics_to_run)} тем(и)…"
                    )
                    for topic, hours in topics_to_run:
                        try:
                            text = await build_news_digest(
                                client, owner_id, topic, hours=hours,
                            )
                            await notifier.notify(f"<b>«{topic}»</b>\n\n{text}")
                        except Exception:
                            logger.exception("news topic failed: %s", topic)
        except Exception:
            logger.exception("news scheduler tick failed")
        await asyncio.sleep(60)
