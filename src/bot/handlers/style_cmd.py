from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from src.bot.filters import OwnerOnly
from src.core.contact_resolver import resolve
from src.core.style_profile import update_style_profile_for_contact
from src.db.repo import get_or_create_user
from src.db.session import get_session
from src.llm.router import build_provider
from src.userbot.manager import UserbotManager


router = Router(name="style_cmd")
router.message.filter(OwnerOnly())


@router.message(Command("style"))
async def cmd_style(message: Message, command: CommandObject, userbot_manager: UserbotManager) -> None:
    client = userbot_manager.get_client(message.from_user.id)
    if client is None:
        await message.answer("Спершу /login.")
        return
    query = (command.args or "").strip()
    if not query:
        await message.answer("Використання: <code>/style ім'я контакту</code> — перерахувати профіль стилю спілкування.")
        return

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        provider = await build_provider(session, owner)

    if provider is None:
        await message.answer("Спершу додай LLM-ключ у /settings.")
        return

    candidates = await resolve(client, owner, query)
    if not candidates:
        await message.answer("Контакт не знайдено.")
        return
    target = candidates[0]
    profile = await update_style_profile_for_contact(provider, message.from_user.id, target.peer_id)
    if not profile:
        await message.answer(
            f"Не знайшов достатньо моїх повідомлень до <b>{target.label()}</b>. "
            "Спершу відкрий /chat і підгрузи історію."
        )
        return

    short = ", ".join(f"{k}: {v}" for k, v in list(profile.items())[:5] if isinstance(v, (str, int)))
    await message.answer(
        f"✅ Профіль стилю для <b>{target.label()}</b> оновлено.\n<i>{short}</i>"
    )
