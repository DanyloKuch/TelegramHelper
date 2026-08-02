from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.filters import OwnerOnly
from src.core.timeutil import fmt_local
from src.db.repo import (
    get_or_create_user,
    list_open_commitments,
    update_commitment_status,
)
from src.db.session import get_session


router = Router(name="todos")
router.message.filter(OwnerOnly())
router.callback_query.filter(OwnerOnly())


def _format(c, tz_name: str) -> str:
    who = "Я" if c.direction == "mine" else (c.peer_name or "Вони")
    deadline = fmt_local(c.deadline_at, tz_name)
    return f"<b>{who}</b> · {c.text} (до {deadline})"


def _kb(commitment_id: int):
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Виконано", callback_data=f"todo:done:{commitment_id}"),
        InlineKeyboardButton(text="🚫 Скасувати", callback_data=f"todo:cancel:{commitment_id}"),
    )
    return kb.as_markup()


@router.message(Command("todos"))
async def cmd_todos(message: Message) -> None:
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        items = await list_open_commitments(session, owner)
        tz_name = owner.settings.timezone

    if not items:
        await message.answer("Відкритих зобов'язань немає 🎉")
        return

    await message.answer(f"📋 Відкритих зобов'язань: <b>{len(items)}</b>")
    for c in items[:30]:
        await message.answer(_format(c, tz_name), reply_markup=_kb(c.id))


@router.callback_query(F.data.startswith("todo:done:"))
async def cb_done(callback: CallbackQuery) -> None:
    cid = int(callback.data.split(":")[2])
    async with get_session() as session:
        await update_commitment_status(session, cid, "done")
    if callback.message:
        await callback.message.edit_text(callback.message.html_text + "\n\n✅ Готово")
    await callback.answer()


@router.callback_query(F.data.startswith("todo:cancel:"))
async def cb_cancel(callback: CallbackQuery) -> None:
    cid = int(callback.data.split(":")[2])
    async with get_session() as session:
        await update_commitment_status(session, cid, "cancelled")
    if callback.message:
        await callback.message.edit_text(callback.message.html_text + "\n\n🚫 Скасовано")
    await callback.answer()
