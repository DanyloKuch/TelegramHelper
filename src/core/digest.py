"""Утренний дайджест: входящие без ответа, горящие обещания и авто-ответы за ночь."""
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from src.config import settings as app_settings
from src.core.notifier import notifier
from src.core.text_sanitizer import sanitize_html
from src.core.timeutil import fmt_local, now_in_tz
from src.db.models import AutoReplyLog, Commitment, Contact, Message, User
from src.db.repo import get_or_create_user, list_open_commitments
from src.db.session import get_session
from src.llm.base import ChatMessage
from src.llm.router import build_provider


logger = logging.getLogger(__name__)


DIGEST_SYSTEM = (
    "Ти робиш короткий ранковий дайджест по моїй Telegram-активності.\n"
    "Структура (HTML aiogram):\n"
    "☀ <b>Доброго ранку!</b>\n\n"
    "📨 <b>Чекають відповіді</b> (якщо є): хто і про що (1 рядок на співрозмовника).\n"
    "🔥 <b>Мої гарячі обіцянки</b>: ті, що протерміновані або в найближчі 24 год.\n"
    "💼 <b>Обіцянки мені</b>: що протерміновано або скоро.\n"
    "🤖 <b>Авто-відповіді</b>: скільки й кому, без подробиць.\n"
    "Якщо в якомусь блоці порожньо — пропускай блок цілком."
)


async def _gather_payload(owner: User) -> dict:
    since = datetime.utcnow() - timedelta(hours=14)

    async with get_session() as session:
        # входящие за период
        incoming_result = await session.execute(
            select(Message)
            .where(
                Message.user_id == owner.id,
                Message.is_outgoing.is_(False),
                Message.date >= since,
            )
            .order_by(Message.date.desc())
            .limit(200)
        )
        incoming = list(incoming_result.scalars().all())

        # сгруппировать по peer и взять только тех, где после последнего входящего нет моего ответа
        by_peer: dict[int, list[Message]] = {}
        for m in incoming:
            by_peer.setdefault(m.peer_id, []).append(m)

        contacts_result = await session.execute(
            select(Contact.peer_id, Contact.display_name).where(Contact.user_id == owner.id)
        )
        contact_names = {peer_id: name for peer_id, name in contacts_result.all()}

        waiting: list[tuple[int, str | None, str]] = []
        for peer_id, msgs in by_peer.items():
            last_in = max(msgs, key=lambda x: x.date)
            my_after = await session.execute(
                select(Message)
                .where(
                    Message.user_id == owner.id,
                    Message.peer_id == peer_id,
                    Message.is_outgoing.is_(True),
                    Message.date > last_in.date,
                )
                .limit(1)
            )
            if my_after.scalar_one_or_none() is None:
                snippet = (last_in.transcript or last_in.text or last_in.extracted_text or "")[:200]
                # назва чату з Contacts, а не sender_name повідомлення: для постів у каналі
                # sender_name — це username каналу (в каналів немає first/last name), а не
                # його реальна назва.
                display_name = contact_names.get(peer_id) or last_in.sender_name
                waiting.append((peer_id, display_name, snippet))

        mine = await list_open_commitments(session, owner, direction="mine")
        theirs = await list_open_commitments(session, owner, direction="theirs")

        autoreplies_result = await session.execute(
            select(AutoReplyLog)
            .where(AutoReplyLog.user_id == owner.id, AutoReplyLog.created_at >= since)
        )
        autoreplies = list(autoreplies_result.scalars().all())

    now = datetime.utcnow()
    soon = now + timedelta(hours=24)

    def _hot(items: list[Commitment]) -> list[Commitment]:
        out = []
        for c in items:
            if c.deadline_at and (c.deadline_at < now or c.deadline_at <= soon):
                out.append(c)
            elif c.deadline_at is None and (now - c.created_at) > timedelta(days=2):
                out.append(c)
        return out

    return {
        "waiting": waiting,
        "mine_hot": _hot(mine),
        "theirs_hot": _hot(theirs),
        "autoreplies": autoreplies,
    }


def _payload_to_text(payload: dict, tz_name: str) -> str:
    parts: list[str] = []
    if payload["waiting"]:
        lines = [f"- {name or peer_id}: {snippet}" for peer_id, name, snippet in payload["waiting"][:20]]
        parts.append("Чекають відповіді:\n" + "\n".join(lines))
    if payload["mine_hot"]:
        lines = []
        for c in payload["mine_hot"][:20]:
            d = fmt_local(c.deadline_at, tz_name) if c.deadline_at else "без срока"
            lines.append(f"- {c.peer_name or c.peer_id}: {c.text} (до {d})")
        parts.append("Мої гарячі обіцянки:\n" + "\n".join(lines))
    if payload["theirs_hot"]:
        lines = []
        for c in payload["theirs_hot"][:20]:
            d = fmt_local(c.deadline_at, tz_name) if c.deadline_at else "без срока"
            lines.append(f"- {c.peer_name or c.peer_id}: {c.text} (до {d})")
        parts.append("Обіцянки мені (гарячі):\n" + "\n".join(lines))
    if payload["autoreplies"]:
        peers = {a.peer_name or a.peer_id for a in payload["autoreplies"]}
        parts.append(f"Авто-відповідей: {len(payload['autoreplies'])} (кому: {', '.join(map(str, peers))})")
    return "\n\n".join(parts) or "Активності не було."


async def build_digest(owner_telegram_id: int) -> str:
    async with get_session() as session:
        owner = await get_or_create_user(session, owner_telegram_id)
        provider = await build_provider(session, owner)
        heavy = owner.settings.use_heavy_model
        tz_name = owner.settings.timezone

    if provider is None:
        return "Не заданий LLM-ключ — не можу зібрати дайджест. Відкрий /settings."

    payload = await _gather_payload(owner)
    raw_text = _payload_to_text(payload, tz_name)
    if raw_text == "Активності не було.":
        return "☀ Доброго ранку! За ніч — тиша."

    response = await provider.chat(
        [
            ChatMessage(role="system", content=DIGEST_SYSTEM),
            ChatMessage(role="user", content=raw_text),
        ],
        heavy=heavy,
    )
    return sanitize_html(response)


async def send_digest(owner_telegram_id: int) -> None:
    text = await build_digest(owner_telegram_id)
    await notifier.notify(text)


async def digest_scheduler_loop() -> None:
    """Каждую минуту проверяет, пора ли отправлять дайджест.
    Сравнение времени — в TZ владельца (UserSettings.timezone)."""
    import asyncio
    last_sent: dict[int, str] = {}  # telegram_id -> "YYYY-MM-DD"
    while True:
        try:
            owner_id = app_settings.owner_telegram_id
            async with get_session() as session:
                owner = await get_or_create_user(session, owner_id)
                tz_name = owner.settings.timezone
                enabled = owner.settings.digest_enabled
                target_hm = owner.settings.digest_time
            local_now = now_in_tz(tz_name)
            current_hm = local_now.strftime("%H:%M")
            current_day = local_now.strftime("%Y-%m-%d")
            if enabled and target_hm == current_hm and last_sent.get(owner_id) != current_day:
                await send_digest(owner_id)
                last_sent[owner_id] = current_day
        except Exception:
            logger.exception("digest scheduler tick failed")
        await asyncio.sleep(60)
