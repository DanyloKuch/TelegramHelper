"""/settings — главное меню и разделы. callback_data: set:sec / set:tog / set:choose / set:input."""
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.filters import OwnerOnly
from src.bot.states import SettingsStates
from src.config import LLMDefaults
from src.core.timeutil import TZ_PRESETS, is_valid_tz, tz_short
from src.db.repo import get_api_key, get_or_create_user, upsert_api_key
from src.db.session import get_session
# Провайдеры импортируются внутри обработчиков ввода ключа: сверху они тянули бы
# оба SDK в память при старте бота.


router = Router(name="settings")
router.message.filter(OwnerOnly())
router.callback_query.filter(OwnerOnly())


HM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _check(value: bool) -> str:
    return "✅" if value else "❌"


# ---------- Главное меню ----------

async def _render_menu(telegram_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with get_session() as session:
        owner = await get_or_create_user(session, telegram_id)
        s = owner.settings
        openai_key = await get_api_key(session, owner, "openai")
        gemini_key = await get_api_key(session, owner, "gemini")

    text = (
        "⚙ <b>Налаштування</b>\n\n"
        f"🌍 Часовий пояс: <b>{tz_short(s.timezone)}</b>\n"
        f"🔄 Авто-відповідь: {_check(s.auto_reply_enabled)} (кулдаун {s.auto_reply_cooldown_min}хв)\n"
        f"☀ Дайджест: {_check(s.digest_enabled)} ({s.digest_time})\n"
        f"⏰ Нагадування: {_check(s.reminders_enabled)} (за {s.reminder_lead_hours}год; протермінування {_check(s.reminder_overdue_enabled)})\n"
        f"📰 Новини: {_check(s.news_enabled)} (вікно {s.news_window_hours}год)\n"
        f"📋 Чек-ін: {_check(s.checkin_enabled)} ({s.checkin_time})\n"
        f"🛡 Ігнорувати архів: {_check(s.ignore_archived)}\n"
        f"🤖 LLM: <b>{s.llm_provider}</b> · {'важка' if s.use_heavy_model else 'легка'}\n"
        f"🎤 Транскрипція: <b>{s.transcription_mode}</b>\n"
        f"🔑 Ключі: OpenAI {_check(bool(openai_key))} · Gemini {_check(bool(gemini_key))}\n\n"
        "<i>Тапни розділ, щоб відкрити його налаштування й опис.</i>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🌍 Часовий пояс", callback_data="set:sec:tz"),
        InlineKeyboardButton(text="🔄 Авто-відповідь", callback_data="set:sec:auto_reply"),
    )
    kb.row(
        InlineKeyboardButton(text="☀ Дайджест", callback_data="set:sec:digest"),
        InlineKeyboardButton(text="⏰ Нагадування", callback_data="set:sec:reminders"),
    )
    kb.row(
        InlineKeyboardButton(text="📰 Новини", callback_data="set:sec:news"),
        InlineKeyboardButton(text="🛡 Приватність", callback_data="set:sec:privacy"),
    )
    kb.row(InlineKeyboardButton(text="📋 Чек-ін", callback_data="set:sec:checkin"))
    kb.row(
        InlineKeyboardButton(text="🤖 LLM", callback_data="set:sec:llm"),
        InlineKeyboardButton(text="🎤 Транскрипція", callback_data="set:sec:transcription"),
    )
    kb.row(InlineKeyboardButton(text="🔑 API-ключі", callback_data="set:sec:keys"))
    kb.row(InlineKeyboardButton(text="❌ Закрити", callback_data="set:close"))
    return text, kb.as_markup()


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    text, kb = await _render_menu(message.from_user.id)
    await message.answer(text, reply_markup=kb)


async def _safe_edit(message, text: str, kb) -> None:
    # глушит безобидное "message is not modified" при повторном тапе той же опции
    if message is None:
        return
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            raise


@router.callback_query(F.data == "set:menu")
async def cb_menu(callback: CallbackQuery) -> None:
    text, kb = await _render_menu(callback.from_user.id)
    await _safe_edit(callback.message, text, kb)
    await callback.answer()


@router.callback_query(F.data == "set:close")
async def cb_close(callback: CallbackQuery) -> None:
    if callback.message:
        await callback.message.delete()
    await callback.answer()


# ---------- Универсальные ручки тогглов и выбора ----------

BOOL_KEYS = {
    "auto_reply_enabled",
    "ignore_archived",
    "digest_enabled",
    "reminders_enabled",
    "reminder_overdue_enabled",
    "news_enabled",
    "use_heavy_model",
    "checkin_enabled",
}

CHOICE_KEYS = {
    "llm_provider": {"openai", "gemini"},
    "transcription_mode": {"local", "api", "modal", "hybrid"},
    "auto_reply_mode": {"static", "smart"},
}

NUMERIC_KEYS = {
    "auto_reply_cooldown_min",
    "reminder_lead_hours",
    "news_window_hours",
}


@router.callback_query(F.data.startswith("set:tog:"))
async def cb_toggle(callback: CallbackQuery) -> None:
    key = callback.data.split(":", 2)[2]
    if key not in BOOL_KEYS:
        await callback.answer("Невідомий перемикач", show_alert=True)
        return
    async with get_session() as session:
        owner = await get_or_create_user(session, callback.from_user.id)
        current = getattr(owner.settings, key)
        setattr(owner.settings, key, not current)
    await callback.answer("Готово")
    await _refresh_section(callback, _section_for_key(key))


@router.callback_query(F.data.startswith("set:choose:"))
async def cb_choose(callback: CallbackQuery) -> None:
    _, _, key, value = callback.data.split(":", 3)
    if key in CHOICE_KEYS:
        if value not in CHOICE_KEYS[key]:
            await callback.answer("Невалідне значення", show_alert=True)
            return
        async with get_session() as session:
            owner = await get_or_create_user(session, callback.from_user.id)
            setattr(owner.settings, key, value)
    elif key in NUMERIC_KEYS:
        try:
            ivalue = max(0, int(value))
        except ValueError:
            await callback.answer("Невалідне число", show_alert=True)
            return
        async with get_session() as session:
            owner = await get_or_create_user(session, callback.from_user.id)
            setattr(owner.settings, key, ivalue)
    else:
        await callback.answer("Невідоме поле", show_alert=True)
        return
    await callback.answer("Готово")
    await _refresh_section(callback, _section_for_key(key))


def _section_for_key(key: str) -> str:
    return {
        "auto_reply_enabled": "auto_reply",
        "auto_reply_cooldown_min": "auto_reply",
        "auto_reply_mode": "auto_reply",
        "auto_reply_text": "auto_reply",
        "ignore_archived": "privacy",
        "digest_enabled": "digest",
        "reminders_enabled": "reminders",
        "reminder_lead_hours": "reminders",
        "reminder_overdue_enabled": "reminders",
        "news_enabled": "news",
        "news_window_hours": "news",
        "checkin_enabled": "checkin",
        "llm_provider": "llm",
        "use_heavy_model": "llm",
        "transcription_mode": "transcription",
    }.get(key, "menu")


async def _refresh_section(callback: CallbackQuery, section: str) -> None:
    if section == "menu":
        text, kb = await _render_menu(callback.from_user.id)
    else:
        text, kb = await _render_section(callback.from_user.id, section)
    await _safe_edit(callback.message, text, kb)


# ---------- Разделы ----------

@router.callback_query(F.data.startswith("set:sec:"))
async def cb_open_section(callback: CallbackQuery) -> None:
    section = callback.data.split(":", 2)[2]
    text, kb = await _render_section(callback.from_user.id, section)
    await _safe_edit(callback.message, text, kb)
    await callback.answer()


def _back_row():
    return [InlineKeyboardButton(text="← Меню налаштувань", callback_data="set:menu")]


async def _render_section(telegram_id: int, section: str) -> tuple[str, InlineKeyboardMarkup]:
    async with get_session() as session:
        owner = await get_or_create_user(session, telegram_id)
        s = owner.settings
        openai_key = await get_api_key(session, owner, "openai")
        gemini_key = await get_api_key(session, owner, "gemini")

    kb = InlineKeyboardBuilder()

    if section == "auto_reply":
        mode_label = "🤖 розумний (LLM у твоєму стилі)" if s.auto_reply_mode == "smart" else "📝 заготовлений текст"
        snippet = (s.auto_reply_text or "").strip().replace("\n", " ")
        if len(snippet) > 80:
            snippet = snippet[:77] + "…"
        text = (
            "🔄 <b>Авто-відповідь</b>\n\n"
            "Коли я <b>офлайн</b> і надходить особисте повідомлення — бот надсилає відповідь.\n"
            "Тільки ЛС, не групи й не боти. Одна відповідь на контакт раз на кулдаун.\n\n"
            "<b>Режими</b>:\n"
            "• <b>заготовлений</b> — надсилається один і той самий текст (нижче).\n"
            "• <b>розумний</b> — LLM пише коротку відповідь у твоєму стилі, опираючись на контекст листування.\n\n"
            f"Статус: <b>{'УВІМК' if s.auto_reply_enabled else 'ВИМК'}</b>\n"
            f"Режим: <b>{mode_label}</b>\n"
            f"Кулдаун: <b>{s.auto_reply_cooldown_min} хв</b>\n"
            f"Текст заготовки:\n<i>«{snippet}»</i>"
        )
        kb.row(InlineKeyboardButton(
            text=f"{_check(s.auto_reply_enabled)} Увімкнути авто-відповідь",
            callback_data="set:tog:auto_reply_enabled",
        ))
        kb.row(
            InlineKeyboardButton(
                text=("• " if s.auto_reply_mode == "static" else "") + "📝 Заготовка",
                callback_data="set:choose:auto_reply_mode:static",
            ),
            InlineKeyboardButton(
                text=("• " if s.auto_reply_mode == "smart" else "") + "🤖 Розумний",
                callback_data="set:choose:auto_reply_mode:smart",
            ),
        )
        kb.row(InlineKeyboardButton(
            text="✏ Змінити текст заготовки",
            callback_data="set:input:auto_reply_text",
        ))
        kb.row(*[
            InlineKeyboardButton(
                text=("• " if s.auto_reply_cooldown_min == m else "") + f"{m}хв",
                callback_data=f"set:choose:auto_reply_cooldown_min:{m}",
            ) for m in (5, 15, 30, 60)
        ])
        kb.row(*_back_row())

    elif section == "digest":
        text = (
            "☀ <b>Ранковий дайджест</b>\n\n"
            "Раз на добу у вказаний час отримую зведення: що сталося за ніч, хто чекає відповіді, "
            "гарячі обіцянки і скільки було авто-відповідей.\n\n"
            f"Статус: <b>{'УВІМК' if s.digest_enabled else 'ВИМК'}</b>\n"
            f"Час: <b>{s.digest_time}</b> · {tz_short(s.timezone)}\n\n"
            "Часовий пояс — окремий розділ у /settings.\n"
            "Для разового зведення — команда /digest"
        )
        kb.row(InlineKeyboardButton(
            text=f"{_check(s.digest_enabled)} Увімкнути дайджест",
            callback_data="set:tog:digest_enabled",
        ))
        kb.row(InlineKeyboardButton(text=f"⏰ Час: {s.digest_time}", callback_data="set:input:digest_time"))
        kb.row(*_back_row())

    elif section == "reminders":
        text = (
            "⏰ <b>Нагадування про дедлайни</b>\n\n"
            "Бот витягує обіцянки з листування (див. /todos і кнопку «Задачі» в /chat) і штовхає, "
            "коли дедлайн близько або протермінований.\n\n"
            f"Статус: <b>{'УВІМК' if s.reminders_enabled else 'ВИМК'}</b>\n"
            f"Заздалегідь за: <b>{s.reminder_lead_hours} год</b>\n"
            f"Алерт про протермінування: <b>{'УВІМК' if s.reminder_overdue_enabled else 'ВИМК'}</b>"
        )
        kb.row(InlineKeyboardButton(
            text=f"{_check(s.reminders_enabled)} Увімкнути нагадування",
            callback_data="set:tog:reminders_enabled",
        ))
        kb.row(InlineKeyboardButton(
            text=f"{_check(s.reminder_overdue_enabled)} Алерт при протермінуванні",
            callback_data="set:tog:reminder_overdue_enabled",
        ))
        kb.row(*[
            InlineKeyboardButton(
                text=("• " if s.reminder_lead_hours == h else "") + f"{h}год",
                callback_data=f"set:choose:reminder_lead_hours:{h}",
            ) for h in (1, 2, 4, 12, 24)
        ])
        kb.row(*_back_row())

    elif section == "news":
        text = (
            "📰 <b>Новини</b>\n\n"
            "Команда <code>/news тема</code> шукає пости у твоїх підписаних каналах за останні N годин і "
            "збирає структурований огляд.\n\n"
            "<b>Авто-новини</b> (цей перемикач): якщо увімкнено, щоранку у вказаний час бот надсилає "
            "дайджест за кожною темою з <b>/news_topics</b>.\n\n"
            "Щоб обмежити вибірку конкретними каналами — /news_channels.\n\n"
            f"Авто-новини: <b>{'УВІМК' if s.news_enabled else 'ВИМК'}</b>\n"
            f"Час надсилання: <b>{s.news_digest_time}</b> · {tz_short(s.timezone)}\n"
            f"Вікно за замовчуванням: <b>{s.news_window_hours} год</b>"
        )
        kb.row(InlineKeyboardButton(
            text=f"{_check(s.news_enabled)} Увімкнути авто-новини",
            callback_data="set:tog:news_enabled",
        ))
        kb.row(InlineKeyboardButton(
            text=f"⏰ Час: {s.news_digest_time}",
            callback_data="set:input:news_time",
        ))
        kb.row(*[
            InlineKeyboardButton(
                text=("• " if s.news_window_hours == h else "") + f"{h}год",
                callback_data=f"set:choose:news_window_hours:{h}",
            ) for h in (6, 12, 24, 48, 72)
        ])
        kb.row(InlineKeyboardButton(text="📋 Теми → /news_topics", callback_data="set:noop:news_topics"))
        kb.row(*_back_row())

    elif section == "llm":
        active = (
            LLMDefaults.OPENAI_CHAT_HEAVY if s.use_heavy_model and s.llm_provider == "openai"
            else LLMDefaults.OPENAI_CHAT_LIGHT if s.llm_provider == "openai"
            else LLMDefaults.GEMINI_CHAT_HEAVY if s.use_heavy_model
            else LLMDefaults.GEMINI_CHAT_LIGHT
        )
        text = (
            "🤖 <b>LLM-провайдер</b>\n\n"
            "Хто відповідає на запити й пише чернетки/самарі. Легка модель — для рутини, "
            "важка — для довгих листувань і складного аналізу.\n\n"
            f"Провайдер: <b>{s.llm_provider}</b>\n"
            f"Режим: <b>{'важка' if s.use_heavy_model else 'легка'}</b>\n"
            f"Активна модель: <code>{active}</code>"
        )
        kb.row(
            InlineKeyboardButton(
                text=("• " if s.llm_provider == "openai" else "") + "OpenAI",
                callback_data="set:choose:llm_provider:openai",
            ),
            InlineKeyboardButton(
                text=("• " if s.llm_provider == "gemini" else "") + "Gemini",
                callback_data="set:choose:llm_provider:gemini",
            ),
        )
        kb.row(InlineKeyboardButton(
            text=f"{_check(s.use_heavy_model)} Важка модель",
            callback_data="set:tog:use_heavy_model",
        ))
        kb.row(*_back_row())

    elif section == "transcription":
        text = (
            "🎤 <b>Транскрипція голосових і аудіо</b>\n\n"
            "<b>local</b> — faster-whisper на твоїй машині (безкоштовно, приватно, потрібні ресурси).\n"
            "<b>api</b> — OpenAI Whisper (~$0.006/хв, потрібен OpenAI key, аудіо йде в OpenAI).\n"
            "<b>modal</b> — свій Whisper large-v3 на Modal GPU: найточніший, але аудіо йде "
            "в хмару і холодний старт займає час.\n"
            "<b>hybrid</b> — local, при помилці запасний шлях (спершу modal, потім OpenAI).\n\n"
            f"Поточний: <b>{s.transcription_mode}</b>"
        )
        for mode in ("local", "api", "modal", "hybrid"):
            kb.row(InlineKeyboardButton(
                text=("• " if s.transcription_mode == mode else "") + mode,
                callback_data=f"set:choose:transcription_mode:{mode}",
            ))
        kb.row(*_back_row())

    elif section == "tz":
        text = (
            "🌍 <b>Часовий пояс</b>\n\n"
            "Від нього залежать:\n"
            "• час ранкового дайджесту й авто-новин\n"
            "• відображення дедлайнів у /todos і нагадуваннях\n"
            "• часові позначки в дайджестах\n\n"
            f"Зараз: <b>{tz_short(s.timezone)}</b>\n\n"
            "Тапни пресет нижче або введи свою IANA-таймзону кнопкою «Інший…»."
        )
        # пресеты по 2 в ряд
        for i in range(0, len(TZ_PRESETS), 2):
            buttons = []
            for tz in TZ_PRESETS[i:i + 2]:
                mark = "• " if s.timezone == tz else ""
                buttons.append(InlineKeyboardButton(text=mark + tz, callback_data=f"set:tz:{tz}"))
            kb.row(*buttons)
        kb.row(InlineKeyboardButton(text="✏ Інший…", callback_data="set:input:timezone"))
        kb.row(*_back_row())

    elif section == "privacy":
        text = (
            "🛡 <b>Приватність і видимість</b>\n\n"
            "Що бот <b>дивиться й обробляє</b> за замовчуванням.\n\n"
            "<b>Ігнорувати архів</b> — чати в архіві Telegram не підгружаються ні в /chat, "
            "ні в /search, ні в /news, ні в авто-відповідь. Увімкнено за замовчуванням.\n\n"
            f"Ігнорувати архів: <b>{'УВІМК' if s.ignore_archived else 'ВИМК'}</b>\n\n"
            "<i>Зміни діють для наступних запитів. Архівний статус підтягується "
            "при /sync.</i>"
        )
        kb.row(InlineKeyboardButton(
            text=f"{_check(s.ignore_archived)} Ігнорувати архів",
            callback_data="set:tog:ignore_archived",
        ))
        kb.row(*_back_row())

    elif section == "checkin":
        text = (
            "📋 <b>Щоденний чек-ін</b>\n\n"
            "Раз на добу у вказаний час нагадую заповнити чек-ін по проєктах — дані летять "
            "окремим рядком у Google Sheets (проєкт, задача, результат, час, коментар). "
            "Кнопка «Звіт зроблено» під нагадуванням дозволяє відмітити день закритим, якщо "
            "заповнив таблицю вручну.\n\n"
            f"Статус: <b>{'УВІМК' if s.checkin_enabled else 'ВИМК'}</b>\n"
            f"Час: <b>{s.checkin_time}</b> · {tz_short(s.timezone)}\n\n"
            "Для разового заповнення поза розкладом — команда /checkin"
        )
        kb.row(InlineKeyboardButton(
            text=f"{_check(s.checkin_enabled)} Увімкнути чек-ін",
            callback_data="set:tog:checkin_enabled",
        ))
        kb.row(InlineKeyboardButton(text=f"⏰ Час: {s.checkin_time}", callback_data="set:input:checkin_time"))
        kb.row(*_back_row())

    elif section == "keys":
        text = (
            "🔑 <b>API-ключі</b>\n\n"
            "Зберігаються зашифрованими (Fernet). Можна перезаписати будь-коли.\n\n"
            f"OpenAI: {_check(bool(openai_key))}\n"
            f"Gemini: {_check(bool(gemini_key))}"
        )
        kb.row(
            InlineKeyboardButton(text="🔑 OpenAI key", callback_data="set:input:openai_key"),
            InlineKeyboardButton(text="🔑 Gemini key", callback_data="set:input:gemini_key"),
        )
        kb.row(*_back_row())

    else:
        text = "Розділ не знайдено."
        kb.row(*_back_row())

    return text, kb.as_markup()


# ---------- FSM-вводы ----------

@router.callback_query(F.data == "set:input:openai_key")
async def cb_input_openai(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_openai_key)
    await callback.message.answer(
        "Надішли OpenAI API key (починається з <code>sk-</code>). Перевірю і збережу. /cancel — скасувати."
    )
    await callback.answer()


@router.callback_query(F.data == "set:input:gemini_key")
async def cb_input_gemini(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_gemini_key)
    await callback.message.answer(
        "Надішли Gemini API key з <code>aistudio.google.com</code>. Перевірю і збережу. /cancel — скасувати."
    )
    await callback.answer()


@router.callback_query(F.data == "set:input:digest_time")
async def cb_input_digest(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_digest_time)
    await callback.message.answer("Введи час у форматі <code>HH:MM</code> (UTC). /cancel — скасувати.")
    await callback.answer()


@router.callback_query(F.data == "set:input:auto_reply_text")
async def cb_input_auto_reply(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_auto_reply_text)
    await callback.message.answer(
        "Надішли новий текст авто-відповіді. Надсилатиметься, коли ти офлайн "
        "(у режимі «заготовка»). /cancel — скасувати."
    )
    await callback.answer()


@router.callback_query(F.data == "set:input:news_time")
async def cb_input_news_time(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_news_time)
    await callback.message.answer(
        "Введи час ранкових авто-новин у <code>HH:MM</code> (UTC). /cancel — скасувати."
    )
    await callback.answer()


@router.callback_query(F.data == "set:input:checkin_time")
async def cb_input_checkin_time(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_checkin_time)
    await callback.message.answer(
        "Введи час щоденного чек-іну у <code>HH:MM</code> (твій часовий пояс). /cancel — скасувати."
    )
    await callback.answer()


@router.callback_query(F.data == "set:noop:news_topics")
async def cb_noop_news_topics(callback: CallbackQuery) -> None:
    await callback.answer("Відкрий /news_topics у меню команд", show_alert=True)


@router.callback_query(F.data.startswith("set:tz:"))
async def cb_pick_tz(callback: CallbackQuery) -> None:
    tz_value = callback.data[len("set:tz:"):]
    if not is_valid_tz(tz_value):
        await callback.answer("Невідомий TZ", show_alert=True)
        return
    async with get_session() as session:
        owner = await get_or_create_user(session, callback.from_user.id)
        owner.settings.timezone = tz_value
    await callback.answer(f"TZ: {tz_value}")
    await _refresh_section(callback, "tz")


@router.callback_query(F.data == "set:input:timezone")
async def cb_input_tz(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_timezone)
    await callback.message.answer(
        "Введи назву часового поясу у форматі IANA, наприклад <code>Europe/Kyiv</code> або "
        "<code>Europe/Warsaw</code>. /cancel — скасувати."
    )
    await callback.answer()


@router.message(SettingsStates.waiting_openai_key)
async def step_openai_key(message: Message, state: FSMContext) -> None:
    key = (message.text or "").strip()
    if not key:
        await message.answer("Порожній ключ. Повтори або /cancel.")
        return
    try:
        await message.delete()
    except Exception:
        pass
    from src.llm.openai_provider import OpenAIProvider

    if not await OpenAIProvider(key).validate_key():
        await message.answer("❌ Ключ не працює. Повтори або /cancel.")
        return
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        await upsert_api_key(session, owner, "openai", key)
    await state.clear()
    await message.answer("✅ OpenAI key збережено.")


@router.message(SettingsStates.waiting_gemini_key)
async def step_gemini_key(message: Message, state: FSMContext) -> None:
    key = (message.text or "").strip()
    if not key:
        await message.answer("Порожній ключ. Повтори або /cancel.")
        return
    try:
        await message.delete()
    except Exception:
        pass
    from src.llm.gemini_provider import GeminiProvider

    if not await GeminiProvider(key).validate_key():
        await message.answer("❌ Ключ не працює. Повтори або /cancel.")
        return
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        await upsert_api_key(session, owner, "gemini", key)
    await state.clear()
    await message.answer("✅ Gemini key збережено.")


@router.message(SettingsStates.waiting_digest_time)
async def step_digest_time(message: Message, state: FSMContext) -> None:
    hm = (message.text or "").strip()
    if not HM_RE.match(hm):
        await message.answer("Формат HH:MM, наприклад <code>06:30</code>. Повтори або /cancel.")
        return
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        owner.settings.digest_time = hm
    await state.clear()
    await message.answer(f"✅ Час дайджесту: <b>{hm} UTC</b>.")


@router.message(SettingsStates.waiting_news_time)
async def step_news_time(message: Message, state: FSMContext) -> None:
    hm = (message.text or "").strip()
    if not HM_RE.match(hm):
        await message.answer("Формат HH:MM, наприклад <code>07:30</code>. Повтори або /cancel.")
        return
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        owner.settings.news_digest_time = hm
        tz = owner.settings.timezone
    await state.clear()
    await message.answer(f"✅ Час авто-новин: <b>{hm}</b> · {tz_short(tz)}.")


@router.message(SettingsStates.waiting_checkin_time)
async def step_checkin_time(message: Message, state: FSMContext) -> None:
    hm = (message.text or "").strip()
    if not HM_RE.match(hm):
        await message.answer("Формат HH:MM, наприклад <code>20:00</code>. Повтори або /cancel.")
        return
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        owner.settings.checkin_time = hm
        tz = owner.settings.timezone
    await state.clear()
    await message.answer(f"✅ Час чек-іну: <b>{hm}</b> · {tz_short(tz)}.")


@router.message(SettingsStates.waiting_auto_reply_text)
async def step_auto_reply_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Порожній текст. Повтори або /cancel.")
        return
    if len(text) > 1000:
        await message.answer("Задовго (макс. 1000 символів). Повтори або /cancel.")
        return
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        owner.settings.auto_reply_text = text
    await state.clear()
    await message.answer(f"✅ Текст авто-відповіді збережено:\n<i>«{text}»</i>")


@router.message(SettingsStates.waiting_timezone)
async def step_timezone(message: Message, state: FSMContext) -> None:
    tz_value = (message.text or "").strip()
    if not is_valid_tz(tz_value):
        await message.answer(
            "Не знайшов такий TZ. Використовуй IANA-формат, наприклад <code>Europe/Kyiv</code>. "
            "Список: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones — "
            "колонка «TZ identifier». /cancel — скасувати."
        )
        return
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        owner.settings.timezone = tz_value
    await state.clear()
    await message.answer(f"✅ Часовий пояс: <b>{tz_short(tz_value)}</b>")
