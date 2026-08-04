import asyncio
import logging

from src.bot.app import run_bot
from src.core.auto_sync import auto_sync_loop
from src.core.checkin import checkin_scheduler_loop
from src.core.digest import digest_scheduler_loop
from src.core.news import news_scheduler_loop
from src.core.reminders import reminders_loop
from src.db.session import init_db
from src.userbot.manager import UserbotManager


logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    logger.info("Starting TelegramAssistant")

    await init_db()

    userbot_manager = UserbotManager()
    await userbot_manager.restore_all()

    bg_tasks = [
        asyncio.create_task(digest_scheduler_loop(), name="digest-scheduler"),
        asyncio.create_task(reminders_loop(), name="reminders-loop"),
        asyncio.create_task(news_scheduler_loop(), name="news-scheduler"),
        asyncio.create_task(auto_sync_loop(), name="auto-sync"),
        asyncio.create_task(checkin_scheduler_loop(), name="checkin-scheduler"),
    ]

    try:
        await run_bot(userbot_manager)
    finally:
        for t in bg_tasks:
            t.cancel()
        for t in bg_tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


def run() -> None:
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown requested")


if __name__ == "__main__":
    run()
