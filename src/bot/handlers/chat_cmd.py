import asyncio
import json
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.filters import OwnerOnly
from src.core.chat_service import load_chat
from src.core.commitment_extractor import extract_and_save_commitments
from src.core.contact_resolver import ContactCandidate, resolve
from src.core.summarizer import answer_question, catchup, draft_reply, summarize_chat
from src.db.repo import get_contact, get_or_create_user
from src.db.session import get_session
from src.llm.router import build_provider
from src.userbot.manager import UserbotManager


logger = logging.getLogger(__name__)
router = Router(name="chat_cmd")
router.message.filter(OwnerOnly())
router.callback_query.filter(OwnerOnly())


def _candidates_keyboard(action: str, candidates: list[ContactCandidate]) -> InlineKeyboardMarkup:
    """Кнопки выбора контакта. callback_data: chat:<action>:<peer_id>"""
    kb = InlineKeyboardBuilder()
    for c in candidates:
        kb.row(InlineKeyboardButton(
            text=f"{c.label()} · {c.score}",
            callback_data=f"chat:{action}:{c.peer_id}",
        ))
    kb.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="chat:cancel:0"))
    return kb.as_markup()


def _actions_keyboard(peer_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📝 Самарі", callback_data=f"chat:summary:{peer_id}"),
        InlineKeyboardButton(text="✅ Задачі/обіцянки", callback_data=f"chat:tasks:{peer_id}"),
    )
    kb.row(
        InlineKeyboardButton(text="💬 Чернетка відповіді", callback_data=f"chat:draft:{peer_id}"),
        InlineKeyboardButton(text="⏪ На чому зупинились", callback_data=f"chat:catchup:{peer_id}"),
    )
    return kb.as_markup()


async def _ensure_client(message: Message, userbot_manager: UserbotManager):
    client = userbot_manager.get_client(message.from_user.id)
    if client is None:
        await message.answer("Спершу підключи акаунт через /login.")
        return None
    return client


@router.message(Command("chat"))
async def cmd_chat(message: Message, command: CommandObject, userbot_manager: UserbotManager) -> None:
    client = await _ensure_client(message, userbot_manager)
    if client is None:
        return

    query = (command.args or "").strip()
    if not query:
        await message.answer("Використання: <code>/chat ім'я або @username</code>")
        return

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)

    candidates = await resolve(client, owner, query)
    if not candidates:
        await message.answer("Не знайшов такого контакту. Уточни ім'я/нік або спробуй /sync.")
        return

    if len(candidates) == 1 or candidates[0].score >= 90:
        await _show_actions(message, candidates[0])
        return

    await message.answer(
        "Кого з них ти мав на увазі?",
        reply_markup=_candidates_keyboard("pick", candidates),
    )


async def _show_actions(message: Message, candidate: ContactCandidate) -> None:
    await message.answer(
        f"Обрано: <b>{candidate.label()}</b>. Що зробити?",
        reply_markup=_actions_keyboard(candidate.peer_id),
    )


@router.callback_query(F.data.startswith("chat:cancel:"))
async def cb_cancel(callback: CallbackQuery) -> None:
    if callback.message:
        await callback.message.edit_text("Скасовано.")
    await callback.answer()


@router.callback_query(F.data.startswith("chat:pick:"))
async def cb_pick(callback: CallbackQuery, userbot_manager: UserbotManager) -> None:
    peer_id = int(callback.data.split(":")[2])
    async with get_session() as session:
        owner = await get_or_create_user(session, callback.from_user.id)
        contact = await get_contact(session, owner, peer_id)
    label = contact.display_name if contact else str(peer_id)
    if callback.message:
        await callback.message.edit_text(
            f"Обрано: <b>{label}</b>. Що зробити?",
            reply_markup=_actions_keyboard(peer_id),
        )
    await callback.answer()


async def _action_load(callback: CallbackQuery, userbot_manager: UserbotManager, peer_id: int):
    """Готовит данные для действий: client, owner, contact, messages, provider."""
    client = userbot_manager.get_client(callback.from_user.id)
    if client is None:
        await callback.answer("Підключи акаунт через /login", show_alert=True)
        return None

    await callback.answer("Підгружаю чат…")
    if callback.message:
        await callback.message.edit_text("⏳ Підгружаю останні повідомлення…")

    messages = await load_chat(client, callback.from_user.id, peer_id, limit=50, transcribe=True)
    async with get_session() as session:
        owner = await get_or_create_user(session, callback.from_user.id)
        contact = await get_contact(session, owner, peer_id)
        provider = await build_provider(session, owner)
        heavy = owner.settings.use_heavy_model

    if contact is None:
        if callback.message:
            await callback.message.edit_text("Контакт не знайдено в локальній БД. Спробуй /sync.")
        return None
    if provider is None:
        if callback.message:
            await callback.message.edit_text(
                "Не заданий API-ключ вибраного LLM. Додай у /settings."
            )
        return None
    return client, owner, contact, messages, provider, heavy


@router.callback_query(F.data.startswith("chat:summary:"))
async def cb_summary(callback: CallbackQuery, userbot_manager: UserbotManager) -> None:
    peer_id = int(callback.data.split(":")[2])
    bundle = await _action_load(callback, userbot_manager, peer_id)
    if bundle is None:
        return
    _client, _owner, contact, messages, provider, heavy = bundle

    text = await summarize_chat(provider, contact, messages, heavy=heavy)
    if callback.message:
        await callback.message.edit_text(
            f"📝 <b>Самарі — {contact.display_name}</b>\n\n{text}",
            reply_markup=_actions_keyboard(peer_id),
        )


@router.callback_query(F.data.startswith("chat:tasks:"))
async def cb_tasks(callback: CallbackQuery, userbot_manager: UserbotManager) -> None:
    peer_id = int(callback.data.split(":")[2])
    bundle = await _action_load(callback, userbot_manager, peer_id)
    if bundle is None:
        return
    _client, owner, contact, messages, provider, _heavy = bundle

    items = await extract_and_save_commitments(
        provider,
        user_id=owner.id,
        contact=contact,
        messages=messages,
    )

    if not items:
        body = "Явних зобов'язань не знайшов."
    else:
        lines = []
        for it in items:
            who = "Я" if it.get("direction") == "mine" else "Вони"
            deadline = it.get("deadline")
            tail = f" · до {deadline}" if deadline else ""
            lines.append(f"• <b>{who}</b>: {it.get('text', '')}{tail}")
        body = "\n".join(lines)

    if callback.message:
        await callback.message.edit_text(
            f"✅ <b>Зобов'язання — {contact.display_name}</b>\n\n{body}",
            reply_markup=_actions_keyboard(peer_id),
        )


@router.callback_query(F.data.startswith("chat:draft:"))
async def cb_draft(callback: CallbackQuery, userbot_manager: UserbotManager) -> None:
    peer_id = int(callback.data.split(":")[2])
    bundle = await _action_load(callback, userbot_manager, peer_id)
    if bundle is None:
        return
    _client, _owner, contact, messages, provider, heavy = bundle

    draft = await draft_reply(provider, contact, messages, heavy=heavy)
    payload = json.dumps({"peer_id": peer_id, "text": draft}, ensure_ascii=False)

    from src.db.repo import create_pending_action
    async with get_session() as session:
        owner = await get_or_create_user(session, callback.from_user.id)
        action = await create_pending_action(
            session, user_id=owner.id, kind="send_message", payload=payload
        )

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Надіслати", callback_data=f"send:confirm:{action.id}"),
        InlineKeyboardButton(text="❌ Скасувати", callback_data=f"send:cancel:{action.id}"),
    )
    if callback.message:
        await callback.message.edit_text(
            f"💬 <b>Чернетка відповіді — {contact.display_name}</b>\n\n{draft}\n\n"
            f"Надіслати?",
            reply_markup=kb.as_markup(),
        )


@router.callback_query(F.data.startswith("chat:catchup:"))
async def cb_catchup(callback: CallbackQuery, userbot_manager: UserbotManager) -> None:
    peer_id = int(callback.data.split(":")[2])
    bundle = await _action_load(callback, userbot_manager, peer_id)
    if bundle is None:
        return
    _client, _owner, contact, messages, provider, heavy = bundle

    text = await catchup(provider, contact, messages, heavy=heavy)
    if callback.message:
        await callback.message.edit_text(
            f"⏪ <b>На чому зупинились — {contact.display_name}</b>\n\n{text}",
            reply_markup=_actions_keyboard(peer_id),
        )


@router.callback_query(F.data.startswith("chat:answer:"))
async def cb_answer(callback: CallbackQuery, userbot_manager: UserbotManager, state: FSMContext) -> None:
    """Обрано контакт для "ask_chat" після неоднозначного пошуку — питання не влазить у
    callback_data, тож переживає вибір через FSM-стан (той самий трюк, що й send.py: send_text)."""
    peer_id = int(callback.data.split(":")[2])
    data = await state.get_data()
    question = data.get("ask_question")
    if not question:
        await callback.answer("Сесію питання втрачено, спитай ще раз", show_alert=True)
        return

    bundle = await _action_load(callback, userbot_manager, peer_id)
    if bundle is None:
        return
    _client, _owner, contact, messages, provider, heavy = bundle

    await state.clear()
    text = await answer_question(provider, contact, messages, question, heavy=heavy)
    if callback.message:
        await callback.message.edit_text(f"💡 <b>{contact.display_name}</b>\n\n{text}")


@router.message(Command("sync"))
async def cmd_sync(message: Message, userbot_manager: UserbotManager) -> None:
    """Sync метаданные диалогов + фоновый prefetch последних сообщений."""
    client = await _ensure_client(message, userbot_manager)
    if client is None:
        return
    from src.userbot.dialogs import prefetch_recent_messages, sync_dialogs

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
    stats = await sync_dialogs(client, owner, limit=500)
    total = sum(stats.values())
    await message.answer(
        f"✅ Синхронізовано {total} діалогів:\n"
        f"  👤 Люди: {stats['users']}\n"
        f"  🤖 Боти: {stats['bots']}\n"
        f"  👥 Групи: {stats['chats']}\n"
        f"  📰 Канали: {stats['channels']}\n"
        f"  🗂 Архівних: {stats['archived']} (за замовчуванням виключаються)\n\n"
        f"⏳ Фоном: підгружаю останні повідомлення з топ-30 активних чатів "
        f"для миттєвого локального пошуку. Це разово, далі все пишеться в реальному часі."
    )

    async def _bg_prefetch() -> None:
        try:
            ps = await prefetch_recent_messages(
                client, message.from_user.id, top_n=30, per_chat=50, skip_channels=False,
            )
            await message.answer(
                f"📥 Prefetch готовий: {ps['chats']} чатів, {ps['messages']} повідомлень у БД."
            )
        except Exception:
            logger.exception("prefetch failed")
            await message.answer("⚠ Prefetch завершився з помилкою — див. логи.")

    asyncio.create_task(_bg_prefetch())
