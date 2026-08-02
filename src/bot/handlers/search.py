import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.filters import OwnerOnly
from src.core.chat_service import load_chat
from src.core.contact_resolver import resolve
from src.core.indexer import index_chat
from src.core.vector_store import vector_store
from src.db.repo import fts_search, get_contact, get_or_create_user, list_contacts
from src.db.session import get_session
from src.llm.router import build_provider
from src.userbot.manager import UserbotManager


logger = logging.getLogger(__name__)
router = Router(name="search")
router.message.filter(OwnerOnly())
router.callback_query.filter(OwnerOnly())


def _result_keyboard(peer_id: int, message_id: int):
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="➡ Переслати мені", callback_data=f"search:fwd:{peer_id}:{message_id}"),
    )
    return kb.as_markup()


@router.message(Command("index"))
async def cmd_index(message: Message, command: CommandObject, userbot_manager: UserbotManager) -> None:
    client = userbot_manager.get_client(message.from_user.id)
    if client is None:
        await message.answer("Спершу /login.")
        return
    query = (command.args or "").strip()
    if not query:
        await message.answer("Використання: <code>/index ім'я контакту</code>")
        return

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
    candidates = await resolve(client, owner, query)
    if not candidates:
        await message.answer("Не знайшов контакт. Спробуй /sync.")
        return
    if len(candidates) > 1 and candidates[0].score < 90:
        kb = InlineKeyboardBuilder()
        for c in candidates:
            kb.row(InlineKeyboardButton(text=f"{c.label()} · {c.score}",
                                        callback_data=f"search:idx:{c.peer_id}"))
        kb.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="search:cancel:0"))
        await message.answer("Кого індексуємо?", reply_markup=kb.as_markup())
        return

    await _do_index(message, candidates[0].peer_id, userbot_manager)


@router.callback_query(F.data.startswith("search:idx:"))
async def cb_idx_pick(callback: CallbackQuery, userbot_manager: UserbotManager) -> None:
    peer_id = int(callback.data.split(":")[2])
    if callback.message:
        await callback.message.edit_text("⏳ Індексую...")
    await _do_index(callback.message, peer_id, userbot_manager, telegram_id=callback.from_user.id)
    await callback.answer()


async def _do_index(message_or_msg, peer_id: int, userbot_manager: UserbotManager,
                    telegram_id: int | None = None) -> None:
    tg_id = telegram_id or message_or_msg.from_user.id
    client = userbot_manager.get_client(tg_id)
    if client is None:
        await message_or_msg.answer("Немає активного userbot. /login.")
        return

    # сначала подтянуть до 500 сообщений в БД
    await load_chat(client, tg_id, peer_id, limit=500, transcribe=True, parse_docs=True)

    async with get_session() as session:
        owner = await get_or_create_user(session, tg_id)
        contact = await get_contact(session, owner, peer_id)
        provider = await build_provider(session, owner)

    if not contact or not provider:
        await message_or_msg.answer("Контакт або LLM-ключ не знайдено.")
        return

    n = await index_chat(provider, owner, contact)
    await message_or_msg.answer(f"✅ Проіндексовано <b>{n}</b> повідомлень у чаті з {contact.display_name}.")


@router.callback_query(F.data == "search:cancel:0")
async def cb_cancel(callback: CallbackQuery) -> None:
    if callback.message:
        await callback.message.edit_text("Скасовано.")
    await callback.answer()


@router.message(Command("search"))
async def cmd_search(message: Message, command: CommandObject, userbot_manager: UserbotManager) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer("Використання: <code>/search текст запиту</code>")
        return

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        provider = await build_provider(session, owner)
        contacts = await list_contacts(session, owner, include_bots=True)
    bot_peer_ids = {c.peer_id for c in contacts if c.is_bot}

    hits_text: list[tuple[int, int, str, str | None, float]] = []  # (peer_id, msg_id, text, peer_name, score)

    if provider is not None:
        try:
            vec = await provider.embed(query)
            vec_hits = await vector_store.search(user_id=owner.id, embedding=vec, limit=16)
            hits_text = [
                (h.peer_id, h.message_id, h.text, h.peer_name, h.score)
                for h in vec_hits
                if h.peer_id not in bot_peer_ids
            ]
        except Exception:
            logger.exception("vector search failed")

    if not hits_text:
        # Fallback: FTS5 по отдельным словам запроса (prefix-match, BM25-ранжирование),
        # а не MATCH всей фразы целиком — так находит и при неточной формулировке.
        contact_by_pid = {c.peer_id: c for c in contacts}
        async with get_session() as session:
            fts_hits = await fts_search(session, owner.id, query, limit=16)
        for h in fts_hits:
            if h.peer_id in bot_peer_ids:
                continue
            c = contact_by_pid.get(h.peer_id)
            hits_text.append((h.peer_id, h.message_id, h.snippet, c.display_name if c else h.sender_name, 0.0))

    if not hits_text:
        await message.answer("Нічого не знайшлося. Спробуй /index &lt;контакт&gt; для індексації.")
        return

    for peer_id, msg_id, body, peer_name, score in hits_text[:8]:
        head = f"<b>{peer_name or peer_id}</b>" + (f" · {score:.2f}" if score else "")
        text = f"{head}\n{body[:600]}"
        await message.answer(text, reply_markup=_result_keyboard(peer_id, msg_id))


@router.callback_query(F.data.startswith("search:fwd:"))
async def cb_forward(callback: CallbackQuery, userbot_manager: UserbotManager) -> None:
    parts = callback.data.split(":")
    peer_id = int(parts[2])
    msg_id = int(parts[3])
    client = userbot_manager.get_client(callback.from_user.id)
    if client is None:
        await callback.answer("Немає userbot, /login", show_alert=True)
        return
    try:
        entity = await client.get_entity(peer_id)
        await client.forward_messages("me", msg_id, entity)
    except Exception:
        logger.exception("forward failed")
        await callback.answer("Не вдалося переслати", show_alert=True)
        return
    await callback.answer("Переслано в Saved Messages")
