"""Трекер часу: проєкти + паралельні запуски (таймери).

Кожен запуск (TimeEntry) незалежний — можна тримати кілька активних одночасно навіть для
одного проєкту. exec_* функції викликаються з free_text.py після LLM-роутингу, решта —
власні command/callback хендлери, за зразком news_topics.py."""
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.filters import OwnerOnly
from src.bot.states import TrackStates
from src.core.project_resolver import resolve_project
from src.core.timeutil import fmt_duration
from src.db.models import TimeEntry
from src.db.repo import (
    get_or_create_project,
    get_or_create_user,
    get_project,
    list_active_entries,
    list_projects,
    start_time_entry,
    stop_time_entry,
)
from src.db.session import get_session


logger = logging.getLogger(__name__)
router = Router(name="track")
router.message.filter(OwnerOnly())
router.callback_query.filter(OwnerOnly())


# ───────────────────────── рендер списку активних запусків ─────────────────────────

def _entry_line(entry: TimeEntry) -> str:
    elapsed = fmt_duration((datetime.utcnow() - entry.started_at).total_seconds())
    note = f" — {entry.note}" if entry.note else ""
    return f"⏱ <b>{entry.project.name}</b>{note} · {elapsed}"


def _stop_button(entry: TimeEntry) -> InlineKeyboardButton:
    label = entry.project.name + (f" ({entry.note})" if entry.note else "")
    if len(label) > 40:
        label = label[:37] + "…"
    return InlineKeyboardButton(text=f"⏹ {label}", callback_data=f"track:stop:{entry.id}")


async def _render(telegram_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with get_session() as session:
        owner = await get_or_create_user(session, telegram_id)
        entries = await list_active_entries(session, owner)

    lines = ["⏱ <b>Активні запуски</b>", ""]
    if not entries:
        lines.append("<i>Зараз нічого не йде. Натисни «➕ Новий запуск».</i>")
    else:
        lines.extend(_entry_line(e) for e in entries)

    kb = InlineKeyboardBuilder()
    for e in entries:
        kb.row(_stop_button(e))
    kb.row(InlineKeyboardButton(text="➕ Новий запуск", callback_data="track:new:0"))
    return "\n".join(lines), kb.as_markup()


async def _refresh(callback: CallbackQuery) -> None:
    text, kb = await _render(callback.from_user.id)
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass


@router.message(Command("track"))
async def cmd_track(message: Message) -> None:
    text, kb = await _render(message.from_user.id)
    await message.answer(text, reply_markup=kb)


# ───────────────────────── старт запуску ─────────────────────────

async def _start_entry(message: Message, telegram_id: int, project_name: str, note: str | None) -> None:
    async with get_session() as session:
        owner = await get_or_create_user(session, telegram_id)
        project = await resolve_project(session, owner, project_name)
        if project is None:
            project = await get_or_create_project(session, owner, project_name)
        await start_time_entry(session, user_id=owner.id, project_id=project.id, note=note)
        final_name = project.name

    extra = f" — {note}" if note else ""
    await message.answer(f"▶ Почав «{final_name}»{extra}.")
    text, kb = await _render(telegram_id)
    await message.answer(text, reply_markup=kb)


async def exec_track_start(intent, message: Message, state: FSMContext, userbot_manager) -> None:
    project_name = (intent.get("project") or "").strip()
    note = (intent.get("note") or "").strip() or None
    if not project_name:
        await message.answer("Який проєкт? Напиши назву.")
        return
    await _start_entry(message, message.from_user.id, project_name, note)


# ───────────────────────── стоп запуску ─────────────────────────

async def _stop_entry(message: Message, entry_id: int) -> None:
    async with get_session() as session:
        entry = await stop_time_entry(session, entry_id, ended_at=datetime.utcnow())
        if entry is not None:
            project_name = entry.project.name
            note = entry.note
            duration = fmt_duration((entry.ended_at - entry.started_at).total_seconds())

    if entry is None:
        await message.answer("Цей запуск вже зупинено або не знайдено.")
        return
    extra = f" ({note})" if note else ""
    await message.answer(f"⏹ Зупинив «{project_name}»{extra} — {duration}.")


async def exec_track_stop(intent, message: Message, state: FSMContext, userbot_manager) -> None:
    project_query = (intent.get("project") or "").strip()

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        active = await list_active_entries(session, owner)

    if not active:
        await message.answer("Зараз нічого не запущено.")
        return

    if project_query:
        active_projects = list({e.project_id: e.project for e in active}.values())
        async with get_session() as session:
            owner = await get_or_create_user(session, message.from_user.id)
            project = await resolve_project(
                session, owner, project_query,
                assume_single=True, candidates=active_projects,
            )
        if project is None:
            names = ", ".join(f"«{p.name}»" for p in active_projects)
            await message.answer(f"Не зрозумів, який проєкт зупинити. Активні: {names}.")
            return
        matched = [e for e in active if e.project_id == project.id]
    else:
        matched = active

    if len(matched) == 1:
        await _stop_entry(message, matched[0].id)
        text, kb = await _render(message.from_user.id)
        await message.answer(text, reply_markup=kb)
        return

    kb = InlineKeyboardBuilder()
    for e in matched:
        kb.row(_stop_button(e))
    await message.answer("Кілька активних — який зупинити?", reply_markup=kb.as_markup())


async def exec_list_active_tracks(intent, message: Message, state: FSMContext, userbot_manager) -> None:
    text, kb = await _render(message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("track:stop:"))
async def cb_stop(callback: CallbackQuery) -> None:
    entry_id = int(callback.data.split(":")[2])
    await _stop_entry(callback.message, entry_id)
    await _refresh(callback)
    await callback.answer("Зупинено")


# ───────────────────────── новий запуск через кнопки ─────────────────────────

@router.callback_query(F.data == "track:new:0")
async def cb_new(callback: CallbackQuery, state: FSMContext) -> None:
    async with get_session() as session:
        owner = await get_or_create_user(session, callback.from_user.id)
        projects = await list_projects(session, owner)

    await state.set_state(TrackStates.waiting_project)
    kb = InlineKeyboardBuilder()
    for p in projects[:8]:
        kb.row(InlineKeyboardButton(text=p.name, callback_data=f"track:pickproj:{p.id}"))

    await callback.message.answer(
        "Який проєкт? Напиши назву (нову — заведеться сама) або обери нижче.",
        reply_markup=kb.as_markup() if projects else None,
    )
    await callback.answer()


@router.message(TrackStates.waiting_project)
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
    await state.set_state(TrackStates.waiting_note)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="➖ Без назви", callback_data="track:skipnote"))
    await message.answer(
        f"Проєкт: <b>{project_name}</b>. Що робитимеш? (або тапни «без назви»)",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("track:pickproj:"))
async def cb_pick_project(callback: CallbackQuery, state: FSMContext) -> None:
    project_id = int(callback.data.split(":")[2])
    async with get_session() as session:
        owner = await get_or_create_user(session, callback.from_user.id)
        project = await get_project(session, project_id)
    if project is None or project.user_id != owner.id:
        await callback.answer("Проєкт не знайдено", show_alert=True)
        return

    await state.set_data({"project_id": project.id, "project_name": project.name})
    await state.set_state(TrackStates.waiting_note)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="➖ Без назви", callback_data="track:skipnote"))
    if callback.message:
        await callback.message.edit_text(
            f"Проєкт: <b>{project.name}</b>. Що робитимеш? (або тапни «без назви»)",
            reply_markup=kb.as_markup(),
        )
    await callback.answer()


async def _finish_start(message: Message, telegram_id: int, state: FSMContext, note: str | None) -> None:
    data = await state.get_data()
    project_id = data.get("project_id")
    project_name = data.get("project_name")
    await state.clear()
    if not project_id:
        await message.answer("Сесію втрачено. Спробуй ще раз через «➕ Новий запуск».")
        return

    async with get_session() as session:
        owner = await get_or_create_user(session, telegram_id)
        await start_time_entry(session, user_id=owner.id, project_id=project_id, note=note)

    extra = f" — {note}" if note else ""
    await message.answer(f"▶ Почав «{project_name}»{extra}.")
    text, kb = await _render(telegram_id)
    await message.answer(text, reply_markup=kb)


@router.message(TrackStates.waiting_note)
async def step_note(message: Message, state: FSMContext) -> None:
    note = (message.text or "").strip() or None
    await _finish_start(message, message.from_user.id, state, note)


@router.callback_query(F.data == "track:skipnote")
async def cb_skip_note(callback: CallbackQuery, state: FSMContext) -> None:
    await _finish_start(callback.message, callback.from_user.id, state, None)
    await callback.answer()
