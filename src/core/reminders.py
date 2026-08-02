"""Напоминания о Commitment'ах: пинги об overdue и о приближении дедлайна."""
import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from src.config import settings as app_settings
from src.core.notifier import notifier
from src.core.timeutil import fmt_local
from src.db.models import Commitment
from src.db.repo import get_or_create_user, update_commitment_status
from src.db.session import get_session


logger = logging.getLogger(__name__)


REMINDER_TICK_SECONDS = 300


async def _check_once(owner_telegram_id: int) -> None:
    async with get_session() as session:
        owner = await get_or_create_user(session, owner_telegram_id)
        s = owner.settings
        if not s.reminders_enabled:
            return

        tz_name = s.timezone
        now = datetime.utcnow()
        lead_hours = max(0, int(s.reminder_lead_hours))
        soon = now + timedelta(hours=lead_hours)

        result = await session.execute(
            select(Commitment).where(
                Commitment.user_id == owner.id,
                Commitment.status == "open",
                Commitment.deadline_at.is_not(None),
            )
        )
        open_items = list(result.scalars().all())

    if not open_items:
        return

    to_remind: list[tuple[Commitment, str]] = []
    for c in open_items:
        d = c.deadline_at
        if d is None:
            continue
        if d < now and s.reminder_overdue_enabled:
            to_remind.append((c, "overdue"))
        elif now <= d <= soon and lead_hours > 0:
            to_remind.append((c, "lead"))

    if not to_remind:
        return

    for commitment, reason in to_remind:
        who = "Я" if commitment.direction == "mine" else (commitment.peer_name or "Вони")
        d = fmt_local(commitment.deadline_at, tz_name)
        if reason == "overdue":
            text = (
                f"⏰ <b>Протерміновано</b>\n"
                f"<b>{who}</b>: {commitment.text}\n"
                f"Термін був: {d}"
            )
        else:
            text = (
                f"⏳ <b>Скоро дедлайн</b>\n"
                f"<b>{who}</b>: {commitment.text}\n"
                f"До: {d}"
            )
        await notifier.notify(text)

    # помечаем reminded чтобы не дублировать
    async with get_session() as session:
        for c, _ in to_remind:
            await update_commitment_status(session, c.id, "reminded")


async def reminders_loop() -> None:
    while True:
        try:
            await _check_once(app_settings.owner_telegram_id)
        except Exception:
            logger.exception("reminders tick failed")
        await asyncio.sleep(REMINDER_TICK_SECONDS)
