import json
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.filters import OwnerOnly
from src.core.contact_resolver import ContactCandidate, resolve
from src.db.repo import (
    create_pending_action,
    delete_pending_action,
    get_contact,
    get_or_create_user,
    get_pending_action,
)
from src.db.session import get_session
from src.llm.base import ChatMessage
from src.llm.router import build_provider
from src.userbot.manager import UserbotManager


logger = logging.getLogger(__name__)
router = Router(name="send")
router.message.filter(OwnerOnly())
router.callback_query.filter(OwnerOnly())


class SendStates(StatesGroup):
    waiting_edit = State()


PARSE_SYSTEM = (
    "Тобі дають вільну фразу-інструкцію на кшталт «скажи Олі, що созвон о 8».\n"
    "Витягни отримувача і текст повідомлення. Повідомлення має бути готове до надсилання "
    "(від першої особи, без префіксів «передай», «скажи»).\n\n"
    'Повертай ЛИШЕ JSON: {"recipient": "...", "message": "..."}.\n'
    "Якщо не вдається визначити — поверни поля null."
)


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        return {}


def _confirm_keyboard(action_id: int):
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Надіслати", callback_data=f"send:confirm:{action_id}"),
        InlineKeyboardButton(text="✏ Змінити", callback_data=f"send:edit:{action_id}"),
    )
    kb.row(InlineKeyboardButton(text="❌ Скасувати", callback_data=f"send:cancel:{action_id}"))
    return kb.as_markup()


def _candidates_keyboard(candidates: list[ContactCandidate], message_text: str):
    """Кнопки выбора получателя для send. callback_data: send:pick:<peer_id>:<action_id>
    Action создаётся уже после выбора, поэтому здесь храним сообщение в коротком кэше через FSM-data."""
    kb = InlineKeyboardBuilder()
    for c in candidates:
        kb.row(InlineKeyboardButton(
            text=f"{c.label()} · {c.score}",
            callback_data=f"send:pick:{c.peer_id}",
        ))
    kb.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="send:cancel:0"))
    return kb.as_markup()


@router.message(Command("send"))
async def cmd_send(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    userbot_manager: UserbotManager,
) -> None:
    client = userbot_manager.get_client(message.from_user.id)
    if client is None:
        await message.answer("Спершу /login.")
        return
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(
            "Використання: <code>/send скажи Олі, що созвон о 8</code>\n"
            "Або: <code>/send @username | текст повідомлення</code>"
        )
        return

    recipient_query: str | None = None
    text: str | None = None

    if "|" in raw:
        parts = raw.split("|", 1)
        recipient_query = parts[0].strip()
        text = parts[1].strip()

    if not recipient_query or not text:
        async with get_session() as session:
            owner = await get_or_create_user(session, message.from_user.id)
            provider = await build_provider(session, owner)
        if provider is None:
            await message.answer("Потрібен LLM-ключ для NL-парсингу. Додай у /settings або використовуй формат «отримувач | текст».")
            return
        parsed_raw = await provider.chat(
            [
                ChatMessage(role="system", content=PARSE_SYSTEM),
                ChatMessage(role="user", content=raw),
            ],
            heavy=False,
        )
        parsed = _parse_json(parsed_raw)
        recipient_query = parsed.get("recipient") or recipient_query
        text = parsed.get("message") or text

    if not recipient_query or not text:
        await message.answer(
            "Не вдалося розібрати запит. Спробуй формат: <code>/send Оля | текст</code>."
        )
        return

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
    candidates = await resolve(client, owner, recipient_query, kinds=("user", "chat", "channel"))
    if not candidates:
        await message.answer(f"Не знайшов контакт «{recipient_query}». Запусти /sync і спробуй знову.")
        return

    if len(candidates) == 1 or candidates[0].score >= 90:
        await _create_and_confirm(message, owner_telegram_id=message.from_user.id,
                                  peer_id=candidates[0].peer_id, text=text,
                                  label=candidates[0].label())
        return

    await state.set_data({"send_text": text})
    await message.answer(
        f"Кому саме надіслати «<i>{text[:80]}</i>»?",
        reply_markup=_candidates_keyboard(candidates, text),
    )


async def _create_and_confirm(
    message: Message,
    *,
    owner_telegram_id: int,
    peer_id: int,
    text: str,
    label: str,
) -> None:
    payload = json.dumps({"peer_id": peer_id, "text": text}, ensure_ascii=False)
    async with get_session() as session:
        owner = await get_or_create_user(session, owner_telegram_id)
        action = await create_pending_action(session, user_id=owner.id, kind="send_message", payload=payload)

    await message.answer(
        f"🤔 <b>Готовий надіслати</b>\n\n"
        f"→ <b>Кому:</b> {label}\n"
        f"→ <b>Текст:</b>\n{text}",
        reply_markup=_confirm_keyboard(action.id),
    )


@router.callback_query(F.data.startswith("send:pick:"))
async def cb_pick(callback: CallbackQuery, state: FSMContext) -> None:
    peer_id = int(callback.data.split(":")[2])
    data = await state.get_data()
    text = data.get("send_text")
    if not text:
        await callback.answer("Сесію втрачено, спробуй /send знову", show_alert=True)
        return
    async with get_session() as session:
        owner = await get_or_create_user(session, callback.from_user.id)
        contact = await get_contact(session, owner, peer_id)
    label = contact.display_name if contact else str(peer_id)

    payload = json.dumps({"peer_id": peer_id, "text": text}, ensure_ascii=False)
    async with get_session() as session:
        owner = await get_or_create_user(session, callback.from_user.id)
        action = await create_pending_action(session, user_id=owner.id, kind="send_message", payload=payload)

    await state.clear()
    if callback.message:
        await callback.message.edit_text(
            f"🤔 <b>Готовий надіслати</b>\n\n"
            f"→ <b>Кому:</b> {label}\n"
            f"→ <b>Текст:</b>\n{text}",
            reply_markup=_confirm_keyboard(action.id),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("send:cancel:"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    action_id = int(parts[2]) if len(parts) > 2 else 0
    if action_id:
        async with get_session() as session:
            await delete_pending_action(session, action_id)
    await state.clear()
    if callback.message:
        await callback.message.edit_text("❌ Надсилання скасовано.")
    await callback.answer()


@router.callback_query(F.data.startswith("send:edit:"))
async def cb_edit(callback: CallbackQuery, state: FSMContext) -> None:
    action_id = int(callback.data.split(":")[2])
    await state.set_state(SendStates.waiting_edit)
    await state.set_data({"action_id": action_id})
    await callback.message.answer("Введи новий текст повідомлення. /cancel — скасувати.")
    await callback.answer()


@router.message(SendStates.waiting_edit)
async def step_edit(message: Message, state: FSMContext) -> None:
    new_text = (message.text or "").strip()
    if not new_text:
        await message.answer("Порожній текст. Повтори або /cancel.")
        return
    data = await state.get_data()
    action_id = data.get("action_id")
    async with get_session() as session:
        action = await get_pending_action(session, action_id)
        if action is None:
            await state.clear()
            await message.answer("Сесію надсилання втрачено. Запусти /send знову.")
            return
        payload = json.loads(action.payload)
        payload["text"] = new_text
        action.payload = json.dumps(payload, ensure_ascii=False)
        peer_id = payload["peer_id"]

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        contact = await get_contact(session, owner, peer_id)
    label = contact.display_name if contact else str(peer_id)

    await state.clear()
    await message.answer(
        f"🤔 <b>Готовий надіслати</b>\n\n→ <b>Кому:</b> {label}\n→ <b>Текст:</b>\n{new_text}",
        reply_markup=_confirm_keyboard(action_id),
    )


@router.callback_query(F.data.startswith("send:confirm:"))
async def cb_confirm(callback: CallbackQuery, userbot_manager: UserbotManager) -> None:
    action_id = int(callback.data.split(":")[2])
    client = userbot_manager.get_client(callback.from_user.id)
    if client is None:
        await callback.answer("Спершу /login", show_alert=True)
        return

    async with get_session() as session:
        action = await get_pending_action(session, action_id)
        if action is None:
            await callback.answer("Дію не знайдено або вже виконано", show_alert=True)
            return
        payload = json.loads(action.payload)
        peer_id = payload["peer_id"]
        text = payload["text"]
        await delete_pending_action(session, action_id)

    try:
        entity = await client.get_entity(peer_id)
        await client.send_message(entity, text)
    except Exception as e:
        logger.exception("send_message failed")
        await callback.answer("Помилка під час надсилання", show_alert=True)
        if callback.message:
            await callback.message.edit_text(f"❌ Не вдалося надіслати: <code>{e}</code>")
        return

    if callback.message:
        await callback.message.edit_text("✅ Повідомлення надіслано.")
    await callback.answer("Надіслано")
