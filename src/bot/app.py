import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from src.bot.handlers import (
    catchup_cmd,
    chat_cmd,
    chat_labels,
    checkin,
    digest_cmd,
    free_text,
    login,
    menu,
    news_cmd,
    news_topics,
    qrlogin,
    search,
    send,
    settings as settings_handlers,
    start,
    style_cmd,
    tasks,
    todos,
    track,
)
from src.config import settings
from src.core.notifier import notifier
from src.userbot.manager import UserbotManager


logger = logging.getLogger(__name__)


async def run_bot(userbot_manager: UserbotManager) -> None:
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    notifier.attach(bot)

    dp = Dispatcher(storage=MemoryStorage())

    dp["userbot_manager"] = userbot_manager

    dp.include_router(start.router)
    dp.include_router(login.router)
    dp.include_router(qrlogin.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(chat_cmd.router)
    dp.include_router(catchup_cmd.router)
    dp.include_router(send.router)
    dp.include_router(search.router)
    dp.include_router(todos.router)
    dp.include_router(digest_cmd.router)
    dp.include_router(style_cmd.router)
    dp.include_router(news_cmd.router)
    dp.include_router(news_topics.router)
    dp.include_router(chat_labels.router)
    dp.include_router(track.router)
    dp.include_router(tasks.router)
    dp.include_router(checkin.router)
    # menu — перед free_text: подписи кнопок приходят обычным текстом
    dp.include_router(menu.router)
    # ВАЖНО: free_text — самым последним, чтобы команды и FSM перехватили текст раньше
    dp.include_router(free_text.router)

    me = await bot.get_me()
    logger.info("Control bot started as @%s", me.username)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
