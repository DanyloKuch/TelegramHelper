"""Щовечірнє нагадування заповнити щоденний чек-ін (Google Sheets).

Патерн 1:1 з digest_scheduler_loop (src/core/digest.py): тік раз на хвилину, порівняння
часу в TZ власника, in-memory дедуп на "той самий день". Додатково звіряємось із
UserSettings.checkin_last_done — щоб не нагадувати, якщо чек-ін уже закритий вручну
(кнопкою «Звіт зроблено» або через сам флоу), навіть якщо процес рестартнув і
in-memory-дедуп обнулився."""
import asyncio
import logging

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.config import settings as app_settings
from src.core.notifier import notifier
from src.core.timeutil import now_in_tz
from src.db.repo import get_or_create_user
from src.db.session import get_session


logger = logging.getLogger(__name__)


def _reminder_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📝 Зробити звіт", callback_data="checkin:start:0"),
        InlineKeyboardButton(text="✅ Звіт зроблено", callback_data="checkin:done_manual:0"),
    )
    return kb.as_markup()


async def send_checkin_reminder() -> None:
    await notifier.notify(
        "📋 Потрібно заповнити щоденний чек-ін.",
        reply_markup=_reminder_kb(),
    )


async def checkin_scheduler_loop() -> None:
    last_sent: dict[int, str] = {}  # telegram_id -> "YYYY-MM-DD"
    while True:
        try:
            owner_id = app_settings.owner_telegram_id
            async with get_session() as session:
                owner = await get_or_create_user(session, owner_id)
                tz_name = owner.settings.timezone
                enabled = owner.settings.checkin_enabled
                target_hm = owner.settings.checkin_time
                last_done = owner.settings.checkin_last_done
            local_now = now_in_tz(tz_name)
            current_hm = local_now.strftime("%H:%M")
            current_day = local_now.strftime("%Y-%m-%d")
            already_done = last_done == current_day
            if (
                enabled
                and target_hm == current_hm
                and last_sent.get(owner_id) != current_day
                and not already_done
            ):
                await send_checkin_reminder()
                last_sent[owner_id] = current_day
        except Exception:
            logger.exception("checkin scheduler tick failed")
        await asyncio.sleep(60)
