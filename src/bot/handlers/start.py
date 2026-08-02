from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from src.bot.filters import OwnerOnly
from src.db.repo import get_or_create_user
from src.db.session import get_session


router = Router(name="start")
router.message.filter(OwnerOnly())


WELCOME = (
    "👋 Я твій AI-асистент для Telegram.\n\n"
    "<b>Акаунт</b>\n"
    "/login — підключити Telegram-акаунт (api_id, api_hash, телефон, код, 2FA)\n"
    "/qrlogin — підключити через QR-код (працює, коли код не приходить)\n"
    "/logout — видалити збережену сесію\n"
    "/sync — оновити список контактів із діалогів\n\n"
    "<b>Налаштування</b>\n"
    "/settings — авто-відповідь, вибір LLM, API-ключі\n\n"
    "<b>Робота з чатами</b>\n"
    "/chat &lt;ім'я&gt; — самарі, задачі, чернетка відповіді, «на чому зупинились»\n"
    "/catchup &lt;ім'я&gt; — на чому зупинились + чернетка відповіді\n"
    "/search &lt;текст&gt; — пошук по проіндексованих повідомленнях\n"
    "/index &lt;ім'я&gt; — проіндексувати чат для семантичного пошуку\n"
    "/send &lt;інструкція&gt; — «скажи Олі, що созвон о 8» (з підтвердженням)\n\n"
    "<b>Новини</b>\n"
    "/news &lt;тема&gt; [--hours=24] — дайджест із підписаних каналів\n"
    "/news_channels — відмітити канали-джерела\n"
    "/news_topics — теми для ранкових авто-новин\n\n"
    "<b>Пам'ять і фічі</b>\n"
    "/todos — відкриті зобов'язання (мої та мені)\n"
    "/digest [now|on|off|at HH:MM] — ранковий дайджест\n"
    "/style &lt;ім'я&gt; — перерахувати профіль мого стилю спілкування з цим контактом\n"
    "/labels — мітки чатів («перешли моє тренування», «допиши в тренування»)\n"
    "/track — трекер часу: проєкти, паралельні запуски\n\n"
    "<b>Меню</b>\n"
    "/menu — показати клавіатуру з основними кнопками\n"
    "/hide_menu — прибрати її\n\n"
    "/help — ця підказка"
)


@router.message(Command("start", "help"))
async def cmd_start(message: Message) -> None:
    from src.bot.handlers.menu import MAIN_KB

    async with get_session() as session:
        await get_or_create_user(session, message.from_user.id)
    await message.answer(WELCOME, reply_markup=MAIN_KB)
