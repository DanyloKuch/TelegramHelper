"""/news_topics — управление темами для утренних авто-новостей."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.filters import OwnerOnly
from src.bot.states import NewsTopicStates
from src.core.timeutil import tz_short
from src.db.repo import (
    add_news_topic,
    delete_news_topic,
    get_or_create_user,
    list_news_topics,
    toggle_news_topic,
)
from src.db.session import get_session


router = Router(name="news_topics")
router.message.filter(OwnerOnly())
router.callback_query.filter(OwnerOnly())


def _check(value: bool) -> str:
    return "✅" if value else "▫"


async def _render(telegram_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with get_session() as session:
        owner = await get_or_create_user(session, telegram_id)
        topics = await list_news_topics(session, owner)
        s = owner.settings

    text_lines = [
        "📰 <b>Теми для авто-новин</b>",
        "",
        f"Авто-новини: <b>{'УВІМК' if s.news_enabled else 'ВИМК'}</b> · щодня о <b>{s.news_digest_time}</b> · {tz_short(s.timezone)}",
        "",
    ]
    if not topics:
        text_lines.append("<i>Тем поки немає. Натисни «➕ Додати тему».</i>")
    else:
        for t in topics:
            text_lines.append(f"{_check(t.enabled)} <b>{t.topic}</b> · вікно {t.hours}год")

    text_lines.append("")
    text_lines.append("<i>Тапни тему, щоб увімк/вимк. Увімкнути авто-новини й час — у /settings → Новини.</i>")

    kb = InlineKeyboardBuilder()
    for t in topics:
        kb.row(
            InlineKeyboardButton(
                text=f"{_check(t.enabled)} {t.topic[:40]}",
                callback_data=f"nt:tog:{t.id}",
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"nt:del:{t.id}"),
        )
    kb.row(InlineKeyboardButton(text="➕ Додати тему", callback_data="nt:add"))
    return "\n".join(text_lines), kb.as_markup()


@router.message(Command("news_topics"))
async def cmd_news_topics(message: Message) -> None:
    text, kb = await _render(message.from_user.id)
    await message.answer(text, reply_markup=kb)


async def _refresh(callback: CallbackQuery) -> None:
    text, kb = await _render(callback.from_user.id)
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass


@router.callback_query(F.data == "nt:add")
async def cb_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(NewsTopicStates.waiting_topic)
    await callback.message.answer(
        "Введи тему однією фразою (можна з вказанням вікна — «AI та регулювання 48»).\n"
        "Якщо число в кінці є — це вікно в годинах (за замовчуванням 24).\n"
        "/cancel — скасувати."
    )
    await callback.answer()


@router.message(NewsTopicStates.waiting_topic)
async def step_topic(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Порожня тема. Повтори або /cancel.")
        return
    parts = raw.rsplit(" ", 1)
    hours = 24
    topic = raw
    if len(parts) == 2 and parts[1].isdigit():
        hours = max(1, min(168, int(parts[1])))
        topic = parts[0].strip()
    if not topic:
        await message.answer("Не схоже на тему. Повтори або /cancel.")
        return

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        await add_news_topic(session, owner, topic, hours=hours)

    await state.clear()
    text, kb = await _render(message.from_user.id)
    await message.answer(f"✅ Додав: <b>{topic}</b> (вікно {hours}год)")
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("nt:tog:"))
async def cb_toggle(callback: CallbackQuery) -> None:
    topic_id = int(callback.data.split(":")[2])
    async with get_session() as session:
        owner = await get_or_create_user(session, callback.from_user.id)
        new_state = await toggle_news_topic(session, owner, topic_id)
    if new_state is None:
        await callback.answer("Тему не знайдено", show_alert=True)
        return
    await _refresh(callback)
    await callback.answer("Увімкнено" if new_state else "Вимкнено")


@router.callback_query(F.data.startswith("nt:del:"))
async def cb_delete(callback: CallbackQuery) -> None:
    topic_id = int(callback.data.split(":")[2])
    async with get_session() as session:
        owner = await get_or_create_user(session, callback.from_user.id)
        ok = await delete_news_topic(session, owner, topic_id)
    if not ok:
        await callback.answer("Не знайдено", show_alert=True)
        return
    await _refresh(callback)
    await callback.answer("Видалено")
