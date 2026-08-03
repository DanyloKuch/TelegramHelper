"""Завдання, згруповані за проєктами (той самий Project, що й у трекері часу).

exec_* функції викликаються з free_text.py після LLM-роутингу, решта —
власні command/callback хендлери, за зразком track.py."""
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.filters import OwnerOnly
from src.bot.states import TaskStates
from src.core.project_resolver import resolve_project
from src.db.models import Task
from src.db.repo import (
    add_task,
    get_or_create_project,
    get_or_create_user,
    get_project,
    list_open_tasks,
    list_projects,
    update_task_status,
)
from src.db.session import get_session


logger = logging.getLogger(__name__)
router = Router(name="tasks")
router.message.filter(OwnerOnly())
router.callback_query.filter(OwnerOnly())


# ───────────────────────── зведення за проєктами ─────────────────────────

def _task_kb(task_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Готово", callback_data=f"task:done:{task_id}"),
        InlineKeyboardButton(text="🚫 Скасувати", callback_data=f"task:cancel:{task_id}"),
    )
    return kb.as_markup()


def _group_by_project(tasks: list[Task]) -> dict[int, list[Task]]:
    by_project: dict[int, list[Task]] = {}
    for t in tasks:
        by_project.setdefault(t.project_id, []).append(t)
    return by_project


async def _render_summary(telegram_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with get_session() as session:
        owner = await get_or_create_user(session, telegram_id)
        tasks = await list_open_tasks(session, owner)

    lines = ["📋 <b>Завдання за проєктами</b>", ""]
    kb = InlineKeyboardBuilder()
    if not tasks:
        lines.append("<i>Завдань немає. Натисни «➕ Нове завдання».</i>")
    else:
        for project_id, items in _group_by_project(tasks).items():
            name = items[0].project.name
            lines.append(f"📁 <b>{name}</b> — {len(items)}")
            kb.row(InlineKeyboardButton(
                text=f"📁 {name} ({len(items)})", callback_data=f"task:proj:{project_id}",
            ))
    kb.row(InlineKeyboardButton(text="➕ Нове завдання", callback_data="task:new:0"))
    return "\n".join(lines), kb.as_markup()


async def _send_project_tasks(message: Message, project_name: str, tasks: list[Task]) -> None:
    await message.answer(f"📁 <b>{project_name}</b> — {len(tasks)} завдань:")
    for t in tasks:
        await message.answer(f"• {t.text}", reply_markup=_task_kb(t.id))


@router.message(Command("tasks"))
async def cmd_tasks(message: Message) -> None:
    text, kb = await _render_summary(message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("task:proj:"))
async def cb_show_project(callback: CallbackQuery) -> None:
    project_id = int(callback.data.split(":")[2])
    async with get_session() as session:
        owner = await get_or_create_user(session, callback.from_user.id)
        tasks = [t for t in await list_open_tasks(session, owner) if t.project_id == project_id]

    if not tasks or callback.message is None:
        await callback.answer("Немає відкритих завдань у цьому проєкті", show_alert=True)
        return
    await _send_project_tasks(callback.message, tasks[0].project.name, tasks)
    await callback.answer()


@router.callback_query(F.data.startswith("task:done:"))
async def cb_done(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":")[2])
    async with get_session() as session:
        await update_task_status(session, task_id, "done")
    if callback.message:
        await callback.message.edit_text(callback.message.html_text + "\n\n✅ Готово")
    await callback.answer()


@router.callback_query(F.data.startswith("task:cancel:"))
async def cb_cancel(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":")[2])
    async with get_session() as session:
        await update_task_status(session, task_id, "cancelled")
    if callback.message:
        await callback.message.edit_text(callback.message.html_text + "\n\n🚫 Скасовано")
    await callback.answer()


# ───────────────────────── нове завдання через кнопки ─────────────────────────

@router.callback_query(F.data == "task:new:0")
async def cb_new(callback: CallbackQuery, state: FSMContext) -> None:
    async with get_session() as session:
        owner = await get_or_create_user(session, callback.from_user.id)
        projects = await list_projects(session, owner)

    await state.set_state(TaskStates.waiting_project)
    kb = InlineKeyboardBuilder()
    for p in projects[:8]:
        kb.row(InlineKeyboardButton(text=p.name, callback_data=f"task:pickproj:{p.id}"))

    await callback.message.answer(
        "Який проєкт? Напиши назву (нову — заведеться сама) або обери нижче.",
        reply_markup=kb.as_markup() if projects else None,
    )
    await callback.answer()


@router.message(TaskStates.waiting_project)
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

    await state.set_data({"project_id": project_id, "project_name": project_name})
    await state.set_state(TaskStates.waiting_text)
    await message.answer(f"Проєкт: <b>{project_name}</b>. Що за завдання?")


@router.callback_query(F.data.startswith("task:pickproj:"))
async def cb_pick_project(callback: CallbackQuery, state: FSMContext) -> None:
    project_id = int(callback.data.split(":")[2])
    async with get_session() as session:
        owner = await get_or_create_user(session, callback.from_user.id)
        project = await get_project(session, project_id)
    if project is None or project.user_id != owner.id:
        await callback.answer("Проєкт не знайдено", show_alert=True)
        return

    await state.set_data({"project_id": project.id, "project_name": project.name})
    await state.set_state(TaskStates.waiting_text)
    if callback.message:
        await callback.message.edit_text(f"Проєкт: <b>{project.name}</b>. Що за завдання?")
    await callback.answer()


@router.message(TaskStates.waiting_text)
async def step_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Порожній текст завдання. Повтори або /cancel.")
        return

    data = await state.get_data()
    project_id = data.get("project_id")
    project_name = data.get("project_name")
    await state.clear()
    if not project_id:
        await message.answer("Сесію втрачено. Спробуй ще раз через «➕ Нове завдання».")
        return

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        await add_task(session, user_id=owner.id, project_id=project_id, text=text)

    await message.answer(f"✅ Додав завдання в «{project_name}»: {text}")


# ───────────────────────── LLM-інтенти ─────────────────────────

async def exec_task_add(intent, message: Message, state: FSMContext, userbot_manager) -> None:
    project_name = (intent.get("project") or "").strip()
    text = (intent.get("text") or "").strip()
    if not project_name or not text:
        await message.answer("Який проєкт і яке завдання? Напиши обидва.")
        return

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        project = await resolve_project(session, owner, project_name)
        if project is None:
            project = await get_or_create_project(session, owner, project_name)
        await add_task(session, user_id=owner.id, project_id=project.id, text=text)
        final_name = project.name

    await message.answer(f"✅ Додав завдання в «{final_name}»: {text}")


async def exec_list_tasks(intent, message: Message, state: FSMContext, userbot_manager) -> None:
    project_query = (intent.get("project") or "").strip()

    if project_query:
        async with get_session() as session:
            owner = await get_or_create_user(session, message.from_user.id)
            project = await resolve_project(session, owner, project_query, assume_single=True)
            tasks = (
                [t for t in await list_open_tasks(session, owner) if t.project_id == project.id]
                if project is not None else []
            )
        if project is None:
            await message.answer(f"Не знайшов проєкт «{project_query}».")
            return
        if not tasks:
            await message.answer(f"У проєкті «{project.name}» немає відкритих завдань.")
            return
        await _send_project_tasks(message, project.name, tasks)
        return

    text, kb = await _render_summary(message.from_user.id)
    await message.answer(text, reply_markup=kb)
