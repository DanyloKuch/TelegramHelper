"""Вход в Telegram-аккаунт по QR-коду.

Нужен потому, что Telegram для сторонних MTProto-клиентов часто возвращает
SentCodeTypeApp, но само сообщение с кодом не доставляет — логин по номеру
залипает навсегда. QR-флоу (auth.exportLoginToken) от доставки кода не зависит.
"""
import asyncio
import io
import logging

import qrcode
from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InputMediaPhoto, Message
from telethon.errors import (
    ApiIdInvalidError,
    PasswordHashInvalidError,
    SessionPasswordNeededError,
)

from src.bot.filters import OwnerOnly
from src.bot.handlers.login import _finalize_login
from src.bot.states import QrLoginStates
from src.db.repo import get_or_create_user, load_telegram_session
from src.db.session import get_session
from src.userbot.manager import UserbotManager


logger = logging.getLogger(__name__)
router = Router(name="qrlogin")
router.message.filter(OwnerOnly())


# QR-токен живёт около минуты, поэтому пересоздаём его по кругу до общего дедлайна.
_TOKEN_WAIT_SECONDS = 30
_TOTAL_DEADLINE_SECONDS = 600

_watchers: dict[int, asyncio.Task] = {}


def cancel_watcher(telegram_id: int) -> None:
    task = _watchers.pop(telegram_id, None)
    if task is not None:
        task.cancel()


def _render_qr(url: str) -> BufferedInputFile:
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return BufferedInputFile(buf.getvalue(), filename="login_qr.png")


CAPTION = (
    "📱 <b>Вхід через QR-код</b>\n\n"
    "На телефоні: <b>Telegram → Налаштування → Пристрої → "
    "Підключити пристрій</b>, потім наведи камеру на цей QR.\n\n"
    "Код оновлюється автоматично. <i>/cancel — скасувати.</i>"
)


@router.message(Command("qrlogin"))
async def cmd_qrlogin(message: Message, state: FSMContext, userbot_manager: UserbotManager) -> None:
    tg_id = message.from_user.id

    async with get_session() as session:
        user = await get_or_create_user(session, tg_id)
        existing = await load_telegram_session(session, user)

    if existing is not None and userbot_manager.get_client(tg_id) is not None:
        await message.answer(
            "Акаунт уже підключено. Спершу виконай /logout, якщо хочеш підключити інший."
        )
        return

    await state.set_state(QrLoginStates.api_id)
    await message.answer(
        "🔐 <b>Підключення через QR-код</b>\n\n"
        "Введи <b>api_id</b> (число) з https://my.telegram.org → API development tools.\n"
        "У будь-який момент можна скасувати командою /cancel."
    )


@router.message(QrLoginStates.api_id)
async def step_api_id(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("api_id — це число. Спробуй ще раз або /cancel.")
        return
    await state.update_data(api_id=int(text))
    await state.set_state(QrLoginStates.api_hash)
    await message.answer("Добре. Тепер введи <b>api_hash</b> (32 hex-символи).")


@router.message(QrLoginStates.api_hash)
async def step_api_hash(
    message: Message, state: FSMContext, userbot_manager: UserbotManager, bot: Bot
) -> None:
    text = (message.text or "").strip()
    if len(text) != 32 or not all(c in "0123456789abcdefABCDEF" for c in text):
        await message.answer("api_hash має бути рядком із 32 hex-символів. Спробуй ще раз або /cancel.")
        return

    data = await state.get_data()
    api_id: int = data["api_id"]
    api_hash: str = text

    tg_id = message.from_user.id
    pending = userbot_manager.start_pending(tg_id, api_id, api_hash)

    try:
        await pending.client.connect()
        qr = await pending.client.qr_login()
    except ApiIdInvalidError:
        await userbot_manager.cancel_pending(tg_id)
        await state.clear()
        await message.answer("❌ api_id/api_hash невірні. Запусти /qrlogin знову.")
        return
    except Exception:
        logger.exception("qr_login init failed")
        await userbot_manager.cancel_pending(tg_id)
        await state.clear()
        await message.answer("❌ Не вдалося почати QR-логін. Запусти /qrlogin знову.")
        return

    qr_message = await message.answer_photo(_render_qr(qr.url), caption=CAPTION)

    await state.set_state(QrLoginStates.waiting_scan)

    old = _watchers.pop(tg_id, None)
    if old is not None:
        old.cancel()

    _watchers[tg_id] = asyncio.create_task(
        _watch_qr(message, state, userbot_manager, bot, qr, qr_message.message_id),
        name=f"qr-login-{tg_id}",
    )


async def _watch_qr(
    message: Message,
    state: FSMContext,
    userbot_manager: UserbotManager,
    bot: Bot,
    qr,
    qr_message_id: int,
) -> None:
    """Ждём сканирования, периодически пересоздавая истёкший токен."""
    tg_id = message.from_user.id
    chat_id = message.chat.id
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _TOTAL_DEADLINE_SECONDS

    try:
        while loop.time() < deadline:
            try:
                await qr.wait(_TOKEN_WAIT_SECONDS)
                break
            except asyncio.TimeoutError:
                await qr.recreate()
                try:
                    await bot.edit_message_media(
                        chat_id=chat_id,
                        message_id=qr_message_id,
                        media=InputMediaPhoto(media=_render_qr(qr.url), caption=CAPTION),
                    )
                except Exception:
                    logger.debug("QR refresh edit failed", exc_info=True)
                continue
            except SessionPasswordNeededError:
                await state.set_state(QrLoginStates.password_2fa)
                await bot.send_message(
                    chat_id,
                    "🔒 QR прийнято, але на акаунті увімкнена двофакторна аутентифікація.\n"
                    "Введи пароль 2FA. Повідомлення з паролем видалю одразу після входу.",
                )
                return
        else:
            await userbot_manager.cancel_pending(tg_id)
            await state.clear()
            await bot.send_message(chat_id, "⌛ QR-код протермінувався. Запусти /qrlogin знову.")
            return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("QR login watcher failed")
        await userbot_manager.cancel_pending(tg_id)
        await state.clear()
        await bot.send_message(chat_id, "❌ Помилка під час QR-логіну. Запусти /qrlogin знову.")
        return
    finally:
        _watchers.pop(tg_id, None)

    await _finalize_login(message, state, userbot_manager)


@router.message(QrLoginStates.waiting_scan)
async def step_waiting(message: Message) -> None:
    await message.answer("Чекаю на сканування QR-коду. /cancel — скасувати.")


@router.message(QrLoginStates.password_2fa)
async def step_2fa(message: Message, state: FSMContext, userbot_manager: UserbotManager) -> None:
    password = (message.text or "").strip()
    if not password:
        await message.answer("Порожній пароль. Введи 2FA-пароль або /cancel.")
        return

    pending = userbot_manager.get_pending(message.from_user.id)
    if pending is None:
        await state.clear()
        await message.answer("Сесія логіну втратилась. Почни заново через /qrlogin.")
        return

    try:
        await pending.client.sign_in(password=password)
    except PasswordHashInvalidError:
        await message.answer("❌ Невірний 2FA-пароль. Спробуй ще раз або /cancel.")
        return
    except Exception:
        logger.exception("QR 2FA sign_in failed")
        await userbot_manager.cancel_pending(message.from_user.id)
        await state.clear()
        await message.answer("❌ Не вдалося увійти. Запусти /qrlogin знову.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    await _finalize_login(message, state, userbot_manager)
