"""Постоянная клавиатура с основными действиями.

Кнопки шлют обычный текст, поэтому этот роутер обязан подключаться РАНЬШЕ
free_text — иначе подписи кнопок уйдут в LLM как свободный запрос.
"""
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from src.bot.filters import OwnerOnly
from src.bot.states import MenuStates
from src.userbot.manager import UserbotManager


logger = logging.getLogger(__name__)
router = Router(name="menu")
router.message.filter(OwnerOnly())


BTN_TODOS = "📋 Зобов'язання"
BTN_DIGEST = "☀ Дайджест"
BTN_SEARCH = "🔎 Пошук"
BTN_CHAT = "💬 Чат"
BTN_NEWS = "📰 Новини"
BTN_SETTINGS = "⚙ Налаштування"
BTN_SYNC = "🔄 Синхронізація"
BTN_HELP = "❓ Довідка"
BTN_TRACKER = "⏱ Трекер"
BTN_TASKS = "📝 Завдання"

ALL_BUTTONS = {
    BTN_TODOS, BTN_DIGEST, BTN_SEARCH, BTN_CHAT,
    BTN_NEWS, BTN_SETTINGS, BTN_SYNC, BTN_HELP, BTN_TRACKER, BTN_TASKS,
}

# Сетка 2 колонки: 10 кнопок.
MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_TODOS), KeyboardButton(text=BTN_CHAT)],
        [KeyboardButton(text=BTN_SEARCH), KeyboardButton(text=BTN_DIGEST)],
        [KeyboardButton(text=BTN_NEWS), KeyboardButton(text=BTN_SETTINGS)],
        [KeyboardButton(text=BTN_SYNC), KeyboardButton(text=BTN_HELP)],
        [KeyboardButton(text=BTN_TRACKER), KeyboardButton(text=BTN_TASKS)],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Напиши задачу або натисни кнопку…",
)


def _cmd(name: str, args: str | None = None) -> CommandObject:
    return CommandObject(prefix="/", command=name, args=args)


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer("Меню розгорнуто. Кнопки нижче.", reply_markup=MAIN_KB)


# «меню» словом — частый способ позвать клавиатуру; без этого фраза уходит в LLM
# как свободный запрос и клавиатура не появляется.
MENU_WORDS = {"меню", "menu", "кнопки", "клавіатура", "клавиатура"}


@router.message(F.text.func(lambda t: (t or "").strip().lower().rstrip("!.?") in MENU_WORDS))
async def txt_menu(message: Message) -> None:
    await message.answer("Меню розгорнуто. Кнопки нижче.", reply_markup=MAIN_KB)


@router.message(Command("hide_menu"))
async def cmd_hide_menu(message: Message) -> None:
    from aiogram.types import ReplyKeyboardRemove

    await message.answer("Клавіатуру прибрано. Повернути — /menu.", reply_markup=ReplyKeyboardRemove())


# ---------- кнопки без аргументов ----------

@router.message(F.text == BTN_TODOS)
async def btn_todos(message: Message) -> None:
    from src.bot.handlers.todos import cmd_todos

    await cmd_todos(message)


@router.message(F.text == BTN_DIGEST)
async def btn_digest(message: Message) -> None:
    from src.bot.handlers.digest_cmd import cmd_digest

    await cmd_digest(message, _cmd("digest", "now"))


@router.message(F.text == BTN_SETTINGS)
async def btn_settings(message: Message) -> None:
    from src.bot.handlers.settings import cmd_settings

    await cmd_settings(message)


@router.message(F.text == BTN_SYNC)
async def btn_sync(message: Message, userbot_manager: UserbotManager) -> None:
    from src.bot.handlers.chat_cmd import cmd_sync

    await cmd_sync(message, userbot_manager)


@router.message(F.text == BTN_HELP)
async def btn_help(message: Message) -> None:
    from src.bot.handlers.start import cmd_start

    await cmd_start(message)


@router.message(F.text == BTN_TRACKER)
async def btn_tracker(message: Message) -> None:
    from src.bot.handlers.track import cmd_track

    await cmd_track(message)


@router.message(F.text == BTN_TASKS)
async def btn_tasks(message: Message) -> None:
    from src.bot.handlers.tasks import cmd_tasks

    await cmd_tasks(message)


# ---------- кнопки, которым нужен аргумент ----------

@router.message(F.text == BTN_SEARCH)
async def btn_search(message: Message, state: FSMContext) -> None:
    await state.set_state(MenuStates.waiting_search)
    await message.answer("Що шукати? Напиши запит (або /cancel).")


@router.message(MenuStates.waiting_search)
async def step_search(message: Message, state: FSMContext, userbot_manager: UserbotManager) -> None:
    from src.bot.handlers.search import cmd_search

    await state.clear()
    await cmd_search(message, _cmd("search", (message.text or "").strip()), userbot_manager)


@router.message(F.text == BTN_CHAT)
async def btn_chat(message: Message, state: FSMContext) -> None:
    await state.set_state(MenuStates.waiting_chat)
    await message.answer("З ким розбираємо чат? Напиши ім'я контакту (або /cancel).")


@router.message(MenuStates.waiting_chat)
async def step_chat(message: Message, state: FSMContext, userbot_manager: UserbotManager) -> None:
    from src.bot.handlers.chat_cmd import cmd_chat

    await state.clear()
    await cmd_chat(message, _cmd("chat", (message.text or "").strip()), userbot_manager)


@router.message(F.text == BTN_NEWS)
async def btn_news(message: Message, state: FSMContext) -> None:
    await state.set_state(MenuStates.waiting_news)
    await message.answer(
        "За якою темою зібрати новини? Можна додати <code>--hours=12</code> (або /cancel)."
    )


@router.message(MenuStates.waiting_news)
async def step_news(message: Message, state: FSMContext, userbot_manager: UserbotManager) -> None:
    from src.bot.handlers.news_cmd import cmd_news

    await state.clear()
    await cmd_news(message, _cmd("news", (message.text or "").strip()), userbot_manager)
