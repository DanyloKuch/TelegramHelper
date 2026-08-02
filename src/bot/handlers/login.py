import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from src.bot.filters import OwnerOnly
from src.bot.states import LoginStates
from src.db.repo import (
    delete_telegram_session,
    get_or_create_user,
    load_telegram_session,
    save_telegram_session,
)
from src.db.session import get_session
from src.userbot.manager import UserbotManager


logger = logging.getLogger(__name__)
router = Router(name="login")
router.message.filter(OwnerOnly())


CANCEL_HINT = "У будь-який момент можна скасувати командою /cancel."


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, userbot_manager: UserbotManager) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer("Нічого скасовувати.")
        return
    # Фоновый watcher QR-логина, если он запущен, надо снять отдельно.
    from src.bot.handlers.qrlogin import cancel_watcher

    cancel_watcher(message.from_user.id)
    await userbot_manager.cancel_pending(message.from_user.id)
    await state.clear()
    await message.answer("Скасовано.")


@router.message(Command("logout"))
async def cmd_logout(message: Message, userbot_manager: UserbotManager) -> None:
    tg_id = message.from_user.id
    await userbot_manager.remove_client(tg_id)
    async with get_session() as session:
        user = await get_or_create_user(session, tg_id)
        await delete_telegram_session(session, user)
    await message.answer("✅ Сесію видалено. Щоб підключитися знову — /login.")


@router.message(Command("login"))
async def cmd_login(message: Message, state: FSMContext, userbot_manager: UserbotManager) -> None:
    tg_id = message.from_user.id

    async with get_session() as session:
        user = await get_or_create_user(session, tg_id)
        existing = await load_telegram_session(session, user)

    if existing is not None and userbot_manager.get_client(tg_id) is not None:
        await message.answer(
            "Акаунт уже підключено. Спершу виконай /logout, якщо хочеш підключити інший."
        )
        return

    await state.set_state(LoginStates.api_id)
    await message.answer(
        "🔐 <b>Підключення Telegram-акаунта</b>\n\n"
        "Отримай <code>api_id</code> і <code>api_hash</code> на https://my.telegram.org → API development tools.\n"
        "Нікому їх не надсилай, крім цього бота. Я зберігаю їх у зашифрованому вигляді.\n\n"
        f"Введи <b>api_id</b> (число).\n{CANCEL_HINT}"
    )


@router.message(LoginStates.api_id)
async def step_api_id(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("api_id — це число. Спробуй ще раз або /cancel.")
        return
    await state.update_data(api_id=int(text))
    await state.set_state(LoginStates.api_hash)
    await message.answer("Добре. Тепер введи <b>api_hash</b> (32 hex-символи).")


@router.message(LoginStates.api_hash)
async def step_api_hash(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) != 32 or not all(c in "0123456789abcdefABCDEF" for c in text):
        await message.answer("api_hash має бути рядком із 32 hex-символів. Спробуй ще раз або /cancel.")
        return
    await state.update_data(api_hash=text)
    await state.set_state(LoginStates.phone)
    await message.answer("Введи номер телефону в міжнародному форматі, наприклад <code>+380671234567</code>.")


@router.message(LoginStates.phone)
async def step_phone(message: Message, state: FSMContext, userbot_manager: UserbotManager) -> None:
    phone = (message.text or "").strip().replace(" ", "")
    if not phone.startswith("+") or not phone[1:].isdigit() or len(phone) < 8:
        await message.answer("Не схоже на телефон. Має бути як <code>+380671234567</code>. /cancel — вийти.")
        return

    data = await state.get_data()
    api_id: int = data["api_id"]
    api_hash: str = data["api_hash"]

    pending = userbot_manager.start_pending(message.from_user.id, api_id, api_hash)
    pending.phone = phone

    try:
        await pending.client.connect()
        sent = await pending.client.send_code_request(phone)
        pending.phone_code_hash = sent.phone_code_hash
    except PhoneNumberInvalidError:
        await userbot_manager.cancel_pending(message.from_user.id)
        await state.clear()
        await message.answer("❌ Telegram сказав: невірний номер. Запусти /login знову.")
        return
    except ApiIdInvalidError:
        await userbot_manager.cancel_pending(message.from_user.id)
        await state.clear()
        await message.answer("❌ api_id/api_hash невірні. Запусти /login знову.")
        return
    except FloodWaitError as e:
        await userbot_manager.cancel_pending(message.from_user.id)
        await state.clear()
        await message.answer(f"❌ FloodWait: почекай {e.seconds} секунд і спробуй /login знову.")
        return
    except Exception:
        logger.exception("send_code_request failed")
        await userbot_manager.cancel_pending(message.from_user.id)
        await state.clear()
        await message.answer("❌ Не вдалося надіслати код. Запусти /login знову.")
        return

    code_type = type(sent.type).__name__
    logger.info("send_code_request ok: type=%s, full=%r", code_type, sent.type)

    where = {
        "SentCodeTypeApp": "у застосунку Telegram — відкрий чат <b>Telegram</b> (service notifications), код там",
        "SentCodeTypeSms": "по SMS на вказаний номер",
        "SentCodeTypeCall": "дзвінком — робот продиктує код",
        "SentCodeTypeFlashCall": "скиданням дзвінка — код у номері того, хто дзвонить",
        "SentCodeTypeMissedCall": "пропущеним дзвінком — код у останніх цифрах номера",
        "SentCodeTypeFragmentSms": "через Fragment (fragment.com)",
        "SentCodeTypeFirebaseSms": "по SMS (Firebase)",
        "SentCodeTypeEmailCode": "на прив'язаний e-mail",
        "SentCodeTypeSmsWord": "по SMS — код це <b>СЛОВО</b>, а не цифри",
        "SentCodeTypeSmsPhrase": "по SMS — код це <b>ФРАЗА</b>, а не цифри",
    }.get(code_type, f"невідомий спосіб ({code_type})")

    await state.update_data(code_type=code_type)
    await state.set_state(LoginStates.code)
    await message.answer(
        f"📨 Код надіслано: {where}.\n"
        f"<i>Тип доставки: {code_type}</i>\n\n"
        "Введи його <b>з пробілами між символами</b>, наприклад: "
        "<code>1 2 3 4 5</code> — інакше Telegram автоматично інвалідує код, "
        "побачивши його відкрито в чаті."
    )


@router.message(LoginStates.code)
async def step_code(message: Message, state: FSMContext, userbot_manager: UserbotManager) -> None:
    raw = (message.text or "").strip()
    data = await state.get_data()
    code_type = data.get("code_type", "")

    if code_type in ("SentCodeTypeSmsWord", "SentCodeTypeSmsPhrase"):
        # Код — слово или фраза: цифр в нём нет, вырезать ничего нельзя.
        code = " ".join(raw.split())
    else:
        code = "".join(ch for ch in raw if ch.isdigit())

    if not code:
        await message.answer("Порожній код. Спробуй ще раз або /cancel.")
        return

    logger.info("sign_in attempt: code_type=%s, code_len=%d", code_type, len(code))

    pending = userbot_manager.get_pending(message.from_user.id)
    if pending is None:
        await state.clear()
        await message.answer("Сесія логіну втратилась. Почни заново через /login.")
        return

    try:
        await pending.client.sign_in(
            phone=pending.phone,
            code=code,
            phone_code_hash=pending.phone_code_hash,
        )
    except SessionPasswordNeededError:
        await state.set_state(LoginStates.password_2fa)
        await message.answer(
            "🔒 На акаунті увімкнена двофакторна аутентифікація. Введи пароль 2FA.\n"
            "Повідомлення з паролем видалю одразу після успішного входу."
        )
        return
    except PhoneCodeInvalidError:
        await message.answer("❌ Невірний код. Спробуй ще раз або /cancel.")
        return
    except PhoneCodeExpiredError:
        await userbot_manager.cancel_pending(message.from_user.id)
        await state.clear()
        await message.answer("❌ Код протермінувався. Запусти /login знову.")
        return
    except Exception:
        logger.exception("sign_in failed")
        await userbot_manager.cancel_pending(message.from_user.id)
        await state.clear()
        await message.answer("❌ Не вдалося увійти. Запусти /login знову.")
        return

    await _finalize_login(message, state, userbot_manager)


@router.message(LoginStates.password_2fa)
async def step_2fa(message: Message, state: FSMContext, userbot_manager: UserbotManager) -> None:
    password = (message.text or "").strip()
    if not password:
        await message.answer("Порожній пароль. Введи 2FA-пароль або /cancel.")
        return

    pending = userbot_manager.get_pending(message.from_user.id)
    if pending is None:
        await state.clear()
        await message.answer("Сесія логіну втратилась. Почни заново через /login.")
        return

    try:
        await pending.client.sign_in(password=password)
    except PasswordHashInvalidError:
        await message.answer("❌ Невірний 2FA-пароль. Спробуй ще раз або /cancel.")
        return
    except Exception:
        logger.exception("2FA sign_in failed")
        await userbot_manager.cancel_pending(message.from_user.id)
        await state.clear()
        await message.answer("❌ Не вдалося увійти. Запусти /login знову.")
        return

    # Удалим сообщение с паролем — гигиена.
    try:
        await message.delete()
    except Exception:
        pass

    await _finalize_login(message, state, userbot_manager)


async def _finalize_login(message: Message, state: FSMContext, userbot_manager: UserbotManager) -> None:
    tg_id = message.from_user.id
    pending = userbot_manager.clear_pending(tg_id)
    if pending is None:
        await state.clear()
        await message.answer("Щось пішло не так. Запусти /login знову.")
        return

    me = await pending.client.get_me()
    label_parts = [p for p in [getattr(me, "first_name", None), getattr(me, "last_name", None)] if p]
    label = " ".join(label_parts) or (me.username or str(me.id))
    session_string = pending.client.session.save()

    async with get_session() as session:
        user = await get_or_create_user(session, tg_id)
        await save_telegram_session(
            session,
            user,
            api_id=pending.api_id,
            api_hash=pending.api_hash,
            session_string=session_string,
            phone=pending.phone or "",
            account_label=label,
        )

    userbot_manager.register_client(tg_id, pending.client)
    await state.clear()
    await message.answer(
        f"✅ Акаунт <b>{label}</b> підключено. Сесію збережено в зашифрованому вигляді.\n\n"
        "Далі — /settings, щоб обрати LLM і налаштувати авто-відповідь."
    )
