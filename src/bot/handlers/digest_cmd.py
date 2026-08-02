import re

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from src.bot.filters import OwnerOnly
from src.core.digest import build_digest
from src.core.timeutil import tz_short
from src.db.repo import get_or_create_user
from src.db.session import get_session


router = Router(name="digest_cmd")
router.message.filter(OwnerOnly())


HM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@router.message(Command("digest"))
async def cmd_digest(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip().lower()

    if not arg or arg == "now":
        text = await build_digest(message.from_user.id)
        await message.answer(text)
        return

    if arg in {"on", "enable", "увімк", "вкл"}:
        async with get_session() as session:
            owner = await get_or_create_user(session, message.from_user.id)
            owner.settings.digest_enabled = True
            tz = owner.settings.timezone
            digest_time = owner.settings.digest_time
        await message.answer(
            f"☀ Дайджест увімкнено. Час: {digest_time} · {tz_short(tz)}.\n"
            "Змінити: /digest at HH:MM або /settings → Дайджест."
        )
        return

    if arg in {"off", "disable", "вимк", "выкл"}:
        async with get_session() as session:
            owner = await get_or_create_user(session, message.from_user.id)
            owner.settings.digest_enabled = False
        await message.answer("Дайджест вимкнено.")
        return

    if arg.startswith("at "):
        hm = arg[3:].strip()
        if not HM_RE.match(hm):
            await message.answer("Формат: <code>/digest at HH:MM</code> (у твоєму TZ, напр. <code>06:30</code>)")
            return
        async with get_session() as session:
            owner = await get_or_create_user(session, message.from_user.id)
            owner.settings.digest_time = hm
            owner.settings.digest_enabled = True
            tz = owner.settings.timezone
        await message.answer(f"☀ Дайджест буде о {hm} щодня · {tz_short(tz)}.")
        return

    await message.answer(
        "Використання:\n"
        "<code>/digest</code> — зібрати зараз\n"
        "<code>/digest on</code> | <code>off</code>\n"
        "<code>/digest at HH:MM</code> (у твоєму часовому поясі)"
    )
