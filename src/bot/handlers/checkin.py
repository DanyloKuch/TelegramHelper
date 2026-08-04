"""Щоденний чек-ін: флоу збору даних (проєкт → задача → результат → час → коментар)
і запис рядка в Google Sheets. Кнопки нагадування (checkin:start/done_manual) —
з src/core/checkin.py; тут — сам флоу, за зразком tasks.py/track.py."""
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.filters import OwnerOnly
from src.bot.states import CheckinStates
from src.core.project_resolver import resolve_project
from src.core.sheets import SheetsNotConfigured, append_checkin_row
from src.core.timeutil import now_in_tz
from src.db.repo import get_or_create_project, get_or_create_user, get_project, list_projects
from src.db.session import get_session


logger = logging.getLogger(__name__)
router = Router(name="checkin")
router.message.filter(OwnerOnly())
router.callback_query.filter(OwnerOnly())


async def _mark_done_today(telegram_id: int) -> None:
    async with get_session() as session:
        owner = await get_or_create_user(session, telegram_id)
        owner.settings.checkin_last_done = now_in_tz(owner.settings.timezone).strftime("%Y-%m-%d")


def _after_save_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="➕ Ще проєкт", callback_data="checkin:another:0"),
        InlineKeyboardButton(text="✅ Все на сьогодні", callback_data="checkin:finish:0"),
    )
    return kb.as_markup()


def _skip_comment_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="➖ Без коментаря", callback_data="checkin:skipcomment"))
    return kb.as_markup()


# ───────────────────────── старт флоу (проєкт) ─────────────────────────

async def _ask_project(message: Message, state: FSMContext, telegram_id: int) -> None:
    async with get_session() as session:
        owner = await get_or_create_user(session, telegram_id)
        projects = await list_projects(session, owner)

    await state.set_state(CheckinStates.waiting_project)
    kb = InlineKeyboardBuilder()
    for p in projects[:8]:
        kb.row(InlineKeyboardButton(text=p.name, callback_data=f"checkin:pickproj:{p.id}"))
    await message.answer(
        "📋 Щоденний чек-ін. Який проєкт? Напиши назву (нову — заведеться сама) або обери нижче.",
        reply_markup=kb.as_markup() if projects else None,
    )


@router.message(Command("checkin"))
async def cmd_checkin(message: Message, state: FSMContext) -> None:
    await _ask_project(message, state, message.from_user.id)


@router.callback_query(F.data == "checkin:start:0")
async def cb_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message:
        await _ask_project(callback.message, state, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "checkin:another:0")
async def cb_another(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message:
        await _ask_project(callback.message, state, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "checkin:done_manual:0")
async def cb_done_manual(callback: CallbackQuery) -> None:
    await _mark_done_today(callback.from_user.id)
    if callback.message:
        await callback.message.edit_text(callback.message.html_text + "\n\n✅ Позначив як зроблено.")
    await callback.answer("Готово")


@router.callback_query(F.data == "checkin:finish:0")
async def cb_finish(callback: CallbackQuery) -> None:
    if callback.message:
        await callback.message.edit_text(callback.message.html_text + "\n\n🌙 На сьогодні все.")
    await callback.answer()


@router.message(CheckinStates.waiting_project)
async def step_project(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Порожня назва. Повтори або /cancel.")
        return

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        project = await resolve_project(session, owner, name)
        if project is None:
            project = await get_or_create_project(session, owner, name)
        project_id, project_name = project.id, project.name

    await state.update_data(project_id=project_id, project_name=project_name)
    await state.set_state(CheckinStates.waiting_task)
    await message.answer(f"Проєкт: <b>{project_name}</b>. Яка задача?")


@router.callback_query(F.data.startswith("checkin:pickproj:"))
async def cb_pick_project(callback: CallbackQuery, state: FSMContext) -> None:
    project_id = int(callback.data.split(":")[2])
    async with get_session() as session:
        owner = await get_or_create_user(session, callback.from_user.id)
        project = await get_project(session, project_id)
    if project is None or project.user_id != owner.id:
        await callback.answer("Проєкт не знайдено", show_alert=True)
        return

    await state.update_data(project_id=project.id, project_name=project.name)
    await state.set_state(CheckinStates.waiting_task)
    if callback.message:
        await callback.message.edit_text(f"Проєкт: <b>{project.name}</b>. Яка задача?")
    await callback.answer()


# ───────────────────────── задача → результат → час → коментар ─────────────────────────

@router.message(CheckinStates.waiting_task)
async def step_task(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Порожньо. Опиши задачу або /cancel.")
        return
    await state.update_data(task=text)
    await state.set_state(CheckinStates.waiting_result)
    await message.answer("Який результат?")


@router.message(CheckinStates.waiting_result)
async def step_result(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Порожньо. Опиши результат або /cancel.")
        return
    await state.update_data(result=text)
    await state.set_state(CheckinStates.waiting_time_spent)
    await message.answer("Скільки часу пішло? (напр. «2 год», «30 хв»)")


@router.message(CheckinStates.waiting_time_spent)
async def step_time_spent(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Порожньо. Напиши, скільки часу пішло, або /cancel.")
        return
    await state.update_data(time_spent=text)
    await state.set_state(CheckinStates.waiting_comment)
    await message.answer("Коментар? (або тапни «без коментаря»)", reply_markup=_skip_comment_kb())


@router.message(CheckinStates.waiting_comment)
async def step_comment(message: Message, state: FSMContext) -> None:
    await _finish_entry(message, state, message.from_user.id, (message.text or "").strip() or None)


@router.callback_query(F.data == "checkin:skipcomment")
async def cb_skip_comment(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message:
        await _finish_entry(callback.message, state, callback.from_user.id, None)
    await callback.answer()


# ───────────────────────── збереження рядка ─────────────────────────

async def _finish_entry(
    message: Message, state: FSMContext, telegram_id: int, comment: str | None,
) -> None:
    data = await state.get_data()
    project_name = data.get("project_name")
    task = data.get("task")
    result = data.get("result")
    time_spent = data.get("time_spent")
    await state.clear()

    if not project_name or not task or not result or not time_spent:
        await message.answer("Сесію втрачено. Спробуй ще раз через /checkin.")
        return

    async with get_session() as session:
        owner = await get_or_create_user(session, telegram_id)
        tz_name = owner.settings.timezone
    date_str = now_in_tz(tz_name).strftime("%Y-%m-%d")

    try:
        await append_checkin_row(
            date_str=date_str,
            project=project_name,
            task=task,
            result=result,
            time_spent=time_spent,
            comment=comment,
        )
    except SheetsNotConfigured:
        logger.warning("checkin: sheets not configured")
        await message.answer(
            "❌ Google Sheets ще не налаштовано (немає GOOGLE_SHEETS_ID/credentials). "
            "Дані не записані — скажи власнику бота підняти конфіг."
        )
        return
    except Exception:
        logger.exception("checkin: failed to append row")
        await message.answer(
            "❌ Не вдалося записати в таблицю (мережа чи доступ). "
            "Спробуй /checkin ще раз за хвилину."
        )
        return

    await _mark_done_today(telegram_id)
    extra = f" — {comment}" if comment else ""
    await message.answer(
        f"✅ Записав у «{project_name}»: {task} → {result} ({time_spent}){extra}",
        reply_markup=_after_save_kb(),
    )
