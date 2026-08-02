"""Killer #4: /catchup <контакт> — на чому зупинились + чернетка відповіді."""
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from src.bot.filters import OwnerOnly
from src.bot.handlers.chat_cmd import _actions_keyboard, _candidates_keyboard
from src.core.contact_resolver import resolve
from src.db.repo import get_or_create_user
from src.db.session import get_session
from src.userbot.manager import UserbotManager


router = Router(name="catchup")
router.message.filter(OwnerOnly())


@router.message(Command("catchup"))
async def cmd_catchup(message: Message, command: CommandObject, userbot_manager: UserbotManager) -> None:
    client = userbot_manager.get_client(message.from_user.id)
    if client is None:
        await message.answer("Спершу /login.")
        return
    query = (command.args or "").strip()
    if not query:
        await message.answer("Використання: <code>/catchup ім'я</code>")
        return
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
    candidates = await resolve(client, owner, query)
    if not candidates:
        await message.answer("Контакт не знайдено. Спробуй /sync.")
        return
    if len(candidates) == 1 or candidates[0].score >= 90:
        # сразу catchup-кнопка через тот же chat:catchup
        await message.answer(
            f"Обрано: <b>{candidates[0].label()}</b>",
            reply_markup=_actions_keyboard(candidates[0].peer_id),
        )
        return
    await message.answer(
        "Кого ти мав на увазі?",
        reply_markup=_candidates_keyboard("pick", candidates),
    )
