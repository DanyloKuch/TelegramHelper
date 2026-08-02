"""Мітки чатів («тренування», «робота») + пов'язані дії: позначити чат, знайти і переслати
своє повідомлення в ньому, дописати фактичне виконання до актуального повідомлення.

Виконавчі функції (exec_*) викликаються з free_text.py після LLM-роутингу; тут же — власні
command/callback хендлери для /labels та підтвердження редагування."""
import json
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.filters import OwnerOnly
from src.core.contact_resolver import resolve
from src.core.label_resolver import resolve_label
from src.core.message_finder import find_message
from src.core.timeutil import now_in_tz
from src.db.repo import (
    create_pending_action,
    delete_pending_action,
    get_contact,
    get_or_create_user,
    get_pending_action,
    list_chat_labels,
    upsert_chat_label,
)
from src.db.session import get_session
from src.userbot.manager import UserbotManager


logger = logging.getLogger(__name__)
router = Router(name="chat_labels")
router.message.filter(OwnerOnly())
router.callback_query.filter(OwnerOnly())

# label_chat/find_and_forward мають працювати з будь-яким типом чату, не лише з людьми.
CHAT_KINDS = ("user", "chat", "channel")


def _candidates_keyboard(candidates):
    kb = InlineKeyboardBuilder()
    for c in candidates:
        kb.row(InlineKeyboardButton(
            text=f"{c.label()} · {c.score}",
            callback_data=f"labelchat:pick:{c.peer_id}",
        ))
    kb.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="labelchat:cancel:0"))
    return kb.as_markup()


def _edit_confirm_keyboard(action_id: int):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"labelchat:editconfirm:{action_id}"))
    kb.row(InlineKeyboardButton(text="❌ Скасувати", callback_data=f"labelchat:editcancel:{action_id}"))
    return kb.as_markup()


# ───────────────────────── label_chat ─────────────────────────

async def exec_label_chat(intent, message: Message, state: FSMContext, userbot_manager: UserbotManager) -> None:
    contact = (intent.get("contact") or "").strip()
    label = (intent.get("label") or "").strip()
    message_prefix = (intent.get("message_prefix") or "").strip() or None
    if not contact or not label:
        await message.answer("Не зрозумів, який чат і яку мітку поставити. Уточни.")
        return

    client = userbot_manager.get_client(message.from_user.id)
    if client is None:
        await message.answer("Спершу /login — потрібен підключений Telegram-акаунт.")
        return

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
    candidates = await resolve(client, owner, contact, kinds=CHAT_KINDS, include_bots=True)
    if not candidates:
        await message.answer(f"Не знайшов чат «{contact}». Спробуй /sync.")
        return

    if len(candidates) > 1 and candidates[0].score < 90:
        await state.set_data({"kind": "label_chat", "label": label, "message_prefix": message_prefix})
        await message.answer(
            f"Який саме чат позначити міткою «{label}»?",
            reply_markup=_candidates_keyboard(candidates),
        )
        return

    target = candidates[0]
    await _save_label(message, target.peer_id, target.label(), label, message_prefix)


async def _save_label(message: Message, peer_id: int, peer_label: str, label: str, message_prefix: str | None) -> None:
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        await upsert_chat_label(
            session, owner,
            peer_id=peer_id, peer_name=peer_label, label=label, message_prefix=message_prefix,
        )
    extra = f"\nПовідомлення там починаються з «{message_prefix}»." if message_prefix else ""
    await message.answer(f"✅ Мітка «{label}» → чат «{peer_label}».{extra}")


# ───────────────────────── find_and_forward ─────────────────────────

async def exec_find_and_forward(intent, message: Message, state: FSMContext, userbot_manager: UserbotManager) -> None:
    label_query = (intent.get("label") or "").strip()
    contact = (intent.get("contact") or "").strip()
    pattern = (intent.get("pattern") or "").strip() or None
    logger.info("find_and_forward intent: label=%r contact=%r pattern=%r", label_query, contact, pattern)

    client = userbot_manager.get_client(message.from_user.id)
    if client is None:
        await message.answer("Спершу /login — потрібен підключений Telegram-акаунт.")
        return

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        all_labels = await list_chat_labels(session, owner)
        label = await resolve_label(session, owner, label_query) if label_query else None
        # LLM міг не витягнути "label" з фрази (напр. "скинь тренування") — якщо мітка лише
        # одна, це майже напевно вона, підстраховуємось так само, як у log_to_label.
        if label is None and not contact and len(all_labels) == 1:
            label = all_labels[0]
    logger.info(
        "find_and_forward resolve: all_labels=%s resolved_label=%s",
        [(l.label, l.peer_name) for l in all_labels],
        (label.label, label.peer_name, label.peer_id) if label else None,
    )

    if label is not None:
        await _forward_from_peer(message, client, label.peer_id, label.peer_name, pattern or label.message_prefix)
        return

    if not contact:
        await message.answer("Не зрозумів, з якого чату переслати. Уточни назву чату або постав мітку.")
        return

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
    candidates = await resolve(client, owner, contact, kinds=CHAT_KINDS, include_bots=True)
    if not candidates:
        await message.answer(f"Не знайшов чат «{contact}». Спробуй /sync.")
        return

    if len(candidates) > 1 and candidates[0].score < 90:
        await state.set_data({"kind": "find_forward", "pattern": pattern})
        await message.answer(
            "З якого чату переслати?",
            reply_markup=_candidates_keyboard(candidates),
        )
        return

    target = candidates[0]
    await _forward_from_peer(message, client, target.peer_id, target.label(), pattern)


async def _forward_from_peer(message: Message, client, peer_id: int, peer_label: str, pattern: str | None) -> None:
    logger.info("forward_from_peer: peer_id=%s peer_label=%r pattern=%r", peer_id, peer_label, pattern)
    try:
        msg = await find_message(client, peer_id, pattern)
    except Exception:
        logger.exception("find_message failed for peer %s", peer_id)
        await message.answer("❌ Не вдалося прочитати чат.")
        return
    logger.info("forward_from_peer: found msg=%s", msg.id if msg else None)

    if msg is None:
        needle = f" з початком «{pattern}»" if pattern else ""
        await message.answer(f"Не знайшов у чаті «{peer_label}» жодного повідомлення{needle}.")
        return

    try:
        bot_me = await message.bot.get_me()
        bot_entity = await client.get_entity(bot_me.username)
        source_entity = await client.get_entity(peer_id)
        await client.forward_messages(bot_entity, msg.id, source_entity)
    except Exception:
        logger.exception("find_and_forward: forward failed")
        await message.answer("❌ Не вдалося переслати повідомлення.")
        return

    await message.answer(f"➡ Переслав повідомлення з «{peer_label}».")


# ───────────────────────── log_to_label (дописати фактичне виконання) ─────────────────────────

async def exec_log_to_label(intent, message: Message, state: FSMContext, userbot_manager: UserbotManager) -> None:
    label_query = (intent.get("label") or "").strip()
    details = (intent.get("details") or "").strip()
    if not details:
        await message.answer("Не зрозумів, що саме записати. Уточни деталі виконання.")
        return

    client = userbot_manager.get_client(message.from_user.id)
    if client is None:
        await message.answer("Спершу /login — потрібен підключений Telegram-акаунт.")
        return

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        labels = await list_chat_labels(session, owner)
        label = await resolve_label(session, owner, label_query) if label_query else None
        if label is None and len(labels) == 1:
            label = labels[0]

    if label is None:
        if not labels:
            await message.answer(
                "Ще немає жодної мітки чату. Спершу познач чат: «постав чату <назва> мітку "
                "тренування, повідомлення там починаються з ...»."
            )
        else:
            names = ", ".join(f"«{l.label}»" for l in labels)
            await message.answer(f"Не зрозумів, до якого чату це відноситься. Наявні мітки: {names}.")
        return

    if not label.message_prefix:
        await message.answer(
            f"Для мітки «{label.label}» не задано, з чого починаються повідомлення. "
            f"Скажи: «у чаті {label.peer_name} повідомлення починаються з ...»."
        )
        return

    await _prepare_edit(message, client, label, details)


async def _prepare_edit(message: Message, client, label, details: str) -> None:
    try:
        msg = await find_message(client, label.peer_id, label.message_prefix)
    except Exception:
        logger.exception("find_message failed for label %s", label.label)
        await message.answer("❌ Не вдалося прочитати чат.")
        return

    if msg is None:
        await message.answer(
            f"Не знайшов у чаті «{label.peer_name}» повідомлення з початком «{label.message_prefix}»."
        )
        return

    # raw_text, не text: без markdown-декорації (Telethon інакше повертає "**Тиждень N**"),
    # і щоб довільні символи з деталей ("*", "_") не зламали парсинг при edit_message.
    original = (msg.raw_text or msg.message or "").strip()

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        tz_name = owner.settings.timezone
    today = now_in_tz(tz_name).strftime("%d.%m")

    addition = f"\n\n✅ Фактично виконано ({today}):\n{details}"
    new_text = original + addition

    payload = json.dumps(
        {"peer_id": label.peer_id, "message_id": msg.id, "new_text": new_text},
        ensure_ascii=False,
    )
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        action = await create_pending_action(session, user_id=owner.id, kind="edit_message", payload=payload)

    preview = original[:500] + ("…" if len(original) > 500 else "")
    await message.answer(
        f"🤔 <b>Готовий дописати в «{label.peer_name}»</b>\n\n"
        f"<i>Поточний текст:</i>\n{preview}\n\n"
        f"<i>Додам:</i>{addition}",
        reply_markup=_edit_confirm_keyboard(action.id),
    )


# ───────────────────────── callbacks ─────────────────────────

@router.callback_query(F.data.startswith("labelchat:pick:"))
async def cb_pick(callback: CallbackQuery, state: FSMContext, userbot_manager: UserbotManager) -> None:
    peer_id = int(callback.data.split(":")[2])
    data = await state.get_data()
    kind = data.get("kind")
    await state.clear()

    client = userbot_manager.get_client(callback.from_user.id)
    if client is None:
        await callback.answer("Немає userbot, /login", show_alert=True)
        return

    async with get_session() as session:
        owner = await get_or_create_user(session, callback.from_user.id)
        contact = await get_contact(session, owner, peer_id)
    peer_label = contact.display_name if contact else str(peer_id)

    if kind == "label_chat":
        if callback.message:
            await callback.message.edit_text("⏳ Зберігаю мітку…")
        await _save_label(callback.message, peer_id, peer_label, data.get("label"), data.get("message_prefix"))
    elif kind == "find_forward":
        if callback.message:
            await callback.message.edit_text("⏳ Шукаю і пересилаю…")
        await _forward_from_peer(callback.message, client, peer_id, peer_label, data.get("pattern"))
    else:
        await callback.answer("Сесію втрачено, спробуй ще раз", show_alert=True)
        return

    await callback.answer()


@router.callback_query(F.data == "labelchat:cancel:0")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if callback.message:
        await callback.message.edit_text("Скасовано.")
    await callback.answer()


@router.callback_query(F.data.startswith("labelchat:editconfirm:"))
async def cb_edit_confirm(callback: CallbackQuery, userbot_manager: UserbotManager) -> None:
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
        await delete_pending_action(session, action_id)

    try:
        entity = await client.get_entity(payload["peer_id"])
        # parse_mode=None: new_text зібраний з raw_text оригіналу + довільних деталей від
        # власника — не хочемо, щоб Telethon намагався розпарсити випадкові "*"/"_" як markdown.
        await client.edit_message(entity, payload["message_id"], payload["new_text"], parse_mode=None)
    except Exception as e:
        logger.exception("edit_message failed")
        if callback.message:
            await callback.message.edit_text(f"❌ Не вдалося відредагувати: <code>{e}</code>")
        await callback.answer("Помилка редагування", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text("✅ Повідомлення оновлено.")
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("labelchat:editcancel:"))
async def cb_edit_cancel(callback: CallbackQuery) -> None:
    action_id = int(callback.data.split(":")[2])
    async with get_session() as session:
        await delete_pending_action(session, action_id)
    if callback.message:
        await callback.message.edit_text("❌ Редагування скасовано.")
    await callback.answer()


# ───────────────────────── /labels ─────────────────────────

@router.message(Command("labels"))
async def cmd_labels(message: Message) -> None:
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        labels = await list_chat_labels(session, owner)
    if not labels:
        await message.answer(
            "Міток ще немає. Скажи щось на кшталт «постав чату &lt;назва&gt; мітку тренування, "
            "повідомлення там починаються з 'Тиждень'»."
        )
        return
    lines = []
    for l in labels:
        extra = f" (початок повідомлень: «{l.message_prefix}»)" if l.message_prefix else ""
        lines.append(f"• <b>{l.label}</b> → {l.peer_name}{extra}")
    await message.answer("🏷 <b>Мітки чатів:</b>\n\n" + "\n".join(lines))
