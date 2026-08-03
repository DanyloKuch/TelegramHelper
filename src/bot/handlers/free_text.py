"""Свободный текст (и голос) → агент → действие. Регистрируется последним в bot/app.py,
чтобы команды и FSM перехватывали свои события раньше."""
import json
import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.filters import OwnerOnly
from src.config import settings as app_settings
from src.core.agent import route_intent
from src.core.chat_service import load_chat
from src.core.commitment_extractor import extract_and_save_commitments
from src.core.contact_resolver import resolve
from src.core.news import build_news_digest
from src.core.summarizer import catchup, draft_reply, summarize_chat
from src.core import conversation_context as ctx_store
from src.core.text_sanitizer import sanitize_html
from src.core.timeutil import fmt_local, is_valid_tz, now_in_tz, tz_short
from src.core.transcription import transcription_service
from src.db.repo import (
    add_commitment,
    add_news_topic,
    create_pending_action,
    delete_news_topic,
    get_api_key,
    get_contact,
    get_or_create_user,
    list_chat_labels,
    list_news_topics,
    list_open_commitments,
    update_commitment_status,
    upsert_contact,
)
from src.db.session import get_session
from src.llm.router import build_provider
from src.userbot.manager import UserbotManager


logger = logging.getLogger(__name__)
router = Router(name="free_text")
router.message.filter(OwnerOnly())


CHAT_LOAD_LIMIT = 50


# Поля UserSettings, которые агент может менять через set_setting (имя → тип значения)
SETTING_FIELDS: dict[str, str] = {
    "auto_reply_enabled":         "bool",
    "auto_reply_mode":            "choice:static,smart",
    "auto_reply_text":            "str",
    "auto_reply_cooldown_min":    "int",
    "digest_enabled":             "bool",
    "digest_time":                "hm",
    "news_enabled":               "bool",
    "news_digest_time":           "hm",
    "news_window_hours":          "int",
    "reminders_enabled":          "bool",
    "reminder_lead_hours":        "int",
    "reminder_overdue_enabled":   "bool",
    "ignore_archived":            "bool",
    "use_heavy_model":            "bool",
    "llm_provider":               "choice:openai,gemini",
    "transcription_mode":         "choice:local,api,modal,hybrid",
    "timezone":                   "tz",
}


import re
_HM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _coerce_setting_value(spec: str, raw):
    if spec == "bool":
        if isinstance(raw, bool):
            return raw, None
        if isinstance(raw, str) and raw.lower() in {"true", "yes", "on", "вкл", "1"}:
            return True, None
        if isinstance(raw, str) and raw.lower() in {"false", "no", "off", "выкл", "0"}:
            return False, None
        return None, "очікую true/false"
    if spec == "int":
        try:
            return int(raw), None
        except (TypeError, ValueError):
            return None, "очікую ціле число"
    if spec == "str":
        if not isinstance(raw, str) or not raw.strip():
            return None, "очікую рядок"
        return raw.strip(), None
    if spec == "hm":
        if isinstance(raw, str) and _HM_RE.match(raw.strip()):
            return raw.strip(), None
        return None, "очікую час у форматі HH:MM"
    if spec == "tz":
        if isinstance(raw, str) and is_valid_tz(raw.strip()):
            return raw.strip(), None
        return None, "не знайшов такий IANA timezone"
    if spec.startswith("choice:"):
        opts = set(spec[len("choice:"):].split(","))
        if isinstance(raw, str) and raw.strip() in opts:
            return raw.strip(), None
        return None, f"допустимі значення: {', '.join(sorted(opts))}"
    return None, "невідомий тип"


def _confirm_keyboard(action_id: int):
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Надіслати", callback_data=f"send:confirm:{action_id}"),
        InlineKeyboardButton(text="✏ Змінити", callback_data=f"send:edit:{action_id}"),
    )
    kb.row(InlineKeyboardButton(text="❌ Скасувати", callback_data=f"send:cancel:{action_id}"))
    return kb.as_markup()


def _candidates_keyboard_send(candidates):
    kb = InlineKeyboardBuilder()
    for c in candidates:
        kb.row(InlineKeyboardButton(
            text=f"{c.label()} · {c.score}",
            callback_data=f"send:pick:{c.peer_id}",
        ))
    kb.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="send:cancel:0"))
    return kb.as_markup()


def _candidates_keyboard_chat(action: str, candidates):
    # action ∈ {summary, tasks, draft, catchup} — re-use chat:* callback'ов из chat_cmd
    kb = InlineKeyboardBuilder()
    for c in candidates:
        kb.row(InlineKeyboardButton(
            text=f"{c.label()} · {c.score}",
            callback_data=f"chat:{action}:{c.peer_id}",
        ))
    kb.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="chat:cancel:0"))
    return kb.as_markup()


async def _execute_intent(intent, message, state, userbot_manager, *, tz_name: str) -> None:
    kind = intent.get("intent")
    client = userbot_manager.get_client(message.from_user.id)

    # selectin-loaded settings/api_keys доступны после закрытия сессии
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        provider = await build_provider(session, owner)
        heavy = owner.settings.use_heavy_model

    if kind == "chat":
        reply = sanitize_html(intent.get("reply"))
        if not reply:
            reply = "Готовий допомогти. Уточни, будь ласка."
        await message.answer(reply)
        return

    if kind == "unknown" or kind is None:
        await message.answer(
            "Не зрозумів, що потрібно зробити. Я вмію: писати повідомлення людям, робити самарі листувань, "
            "витягувати задачі, ловити «на чому зупинились», шукати по повідомленнях, збирати новинний "
            "дайджест за темою, показувати обіцянки. Спробуй сформулювати інакше або відкрий /help."
        )
        return

    if kind == "list_todos":
        async with get_session() as session:
            owner = await get_or_create_user(session, message.from_user.id)
            items = await list_open_commitments(session, owner)
        if not items:
            await message.answer("Відкритих зобов'язань немає 🎉")
            return
        from src.core.timeutil import fmt_local
        lines = []
        for c in items[:30]:
            who = "Я" if c.direction == "mine" else (c.peer_name or "Вони")
            d = fmt_local(c.deadline_at, tz_name)
            lines.append(f"• <b>{who}</b>: {c.text} (до {d})")
        await message.answer(
            f"📋 Відкритих зобов'язань: <b>{len(items)}</b>\n\n" + "\n".join(lines)
        )
        return

    if client is None:
        await message.answer("Спершу /login — потрібен підключений Telegram-акаунт.")
        return

    if kind == "send_message":
        recipient = (intent.get("recipient") or "").strip()
        text = (intent.get("text") or "").strip()
        if not recipient or not text:
            await message.answer("Не вистачає кому/що надіслати. Уточни.")
            return
        candidates = await resolve(client, owner, recipient)
        if not candidates:
            await message.answer(f"Не знайшов контакт «{recipient}». Спробуй /sync.")
            return
        if len(candidates) == 1 or candidates[0].score >= 90:
            target = candidates[0]
            ctx_store.set_last_peer(message.from_user.id, target.peer_id, target.display_name)
            payload = json.dumps({"peer_id": target.peer_id, "text": text}, ensure_ascii=False)
            async with get_session() as session:
                owner = await get_or_create_user(session, message.from_user.id)
                action = await create_pending_action(
                    session, user_id=owner.id, kind="send_message", payload=payload
                )
            await message.answer(
                f"🤔 <b>Готовий надіслати</b>\n\n"
                f"→ <b>Кому:</b> {target.label()}\n"
                f"→ <b>Текст:</b>\n{text}",
                reply_markup=_confirm_keyboard(action.id),
            )
        else:
            await state.set_data({"send_text": text})
            await message.answer(
                f"Кому саме надіслати «<i>{text[:80]}</i>»?",
                reply_markup=_candidates_keyboard_send(candidates),
            )
        return

    if kind == "search":
        query = (intent.get("query") or "").strip() or raw
        await message.answer(f"🔎 Шукаю: <i>{query}</i>…")
        from src.bot.handlers.search import cmd_search
        from aiogram.filters import CommandObject
        await cmd_search(message, CommandObject(prefix="/", command="search", args=query), userbot_manager)
        return

    if kind == "find_in_chats":
        query = (intent.get("query") or "").strip()
        action = (intent.get("action") or "catchup").strip()
        if action not in {"catchup", "summary", "tasks", "draft"}:
            action = "catchup"
        if not query:
            await message.answer("Не зрозумів, за якою темою шукати.")
            return
        await message.answer(f"🔎 Шукаю по моїх чатах: «<i>{query}</i>»…")
        await _find_chats_and_offer(message, client, query, action)
        return

    if kind == "news_digest":
        topic = (intent.get("topic") or "").strip()
        if not topic:
            await message.answer("Уточни тему для новин.")
            return
        try:
            hours = int(intent.get("hours") or 24)
        except (TypeError, ValueError):
            hours = 24
        hours = max(1, min(168, hours))
        await message.answer(f"📰 Готую дайджест: <i>{topic}</i> · вікно {hours}год…")
        text = await build_news_digest(client, message.from_user.id, topic, hours=hours)
        await message.answer(text, disable_web_page_preview=True)
        return

    # ниже — интенты, требующие конкретного контакта
    contact_query = (intent.get("contact") or "").strip()
    if not contact_query:
        await message.answer("Не зрозумів, з яким контактом працювати. Уточни ім'я.")
        return

    # kinds розширено на групи/канали: "contact" тут часто — назва чату/каналу з мітки
    # (напр. «Video: розробка порталу»), а не людина.
    candidates = await resolve(client, owner, contact_query, kinds=("user", "chat", "channel"))
    if not candidates:
        await message.answer(f"Не знайшов контакт «{contact_query}». Спробуй /sync.")
        return

    action_map = {
        "summarize_chat": "summary",
        "tasks_for_chat": "tasks",
        "draft_reply":    "draft",
        "catchup":        "catchup",
    }
    cb_action = action_map.get(kind)
    if cb_action is None:
        await message.answer("Невідома дія.")
        return

    if len(candidates) > 1 and candidates[0].score < 90:
        await message.answer(
            f"З ким саме? (дія: <b>{cb_action}</b>)",
            reply_markup=_candidates_keyboard_chat(cb_action, candidates),
        )
        return

    target = candidates[0]
    ctx_store.set_last_peer(message.from_user.id, target.peer_id, target.display_name)
    await message.answer(f"⏳ Підгружаю чат з <b>{target.label()}</b>…")
    messages_loaded = await load_chat(
        client, message.from_user.id, target.peer_id,
        limit=CHAT_LOAD_LIMIT, transcribe=True,
    )
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        contact = await get_contact(session, owner, target.peer_id)
        provider = await build_provider(session, owner)
        heavy = owner.settings.use_heavy_model

    if contact is None or provider is None:
        await message.answer("Не вдалося підготувати контекст.")
        return

    if kind == "summarize_chat":
        text = await summarize_chat(provider, contact, messages_loaded, heavy=heavy)
        await message.answer(f"📝 <b>Самарі — {contact.display_name}</b>\n\n{text}")

    elif kind == "tasks_for_chat":
        items = await extract_and_save_commitments(
            provider, user_id=owner.id, contact=contact, messages=messages_loaded
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
        await message.answer(f"✅ <b>Зобов'язання — {contact.display_name}</b>\n\n{body}")

    elif kind == "draft_reply":
        instruction = intent.get("instruction") or None
        draft = await draft_reply(provider, contact, messages_loaded, instruction=instruction, heavy=heavy)
        payload = json.dumps({"peer_id": target.peer_id, "text": draft}, ensure_ascii=False)
        async with get_session() as session:
            owner = await get_or_create_user(session, message.from_user.id)
            action = await create_pending_action(
                session, user_id=owner.id, kind="send_message", payload=payload
            )
        await message.answer(
            f"💬 <b>Чернетка — {contact.display_name}</b>\n\n{draft}\n\nНадіслати?",
            reply_markup=_confirm_keyboard(action.id),
        )

    elif kind == "catchup":
        text = await catchup(provider, contact, messages_loaded, heavy=heavy)
        await message.answer(
            f"⏪ <b>На чому зупинились — {contact.display_name}</b>\n\n{text}"
        )


async def _find_chats_and_offer(message, client, query: str, action: str) -> None:
    from src.core.chat_finder import smart_find

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        provider = await build_provider(session, owner)
    if provider is None:
        await message.answer("Потрібен LLM-ключ (/settings → 🔑).")
        return

    try:
        results = await smart_find(client, owner, provider, query, top_n=5)
    except Exception:
        logger.exception("smart_find failed")
        await message.answer("❌ Пошук не вдався. Спробуй ще раз або уточни запит.")
        return

    if not results:
        await message.answer(
            f"Нічого не знайшов за «{query}» — ні за текстом, ні за іменами контактів. "
            "Спробуй описати трохи конкретніше або назви сам контакт."
        )
        return

    # пишем контакты в БД, чтобы chat:<action>:<peer_id> handler знал display_name
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        for r in results:
            await upsert_contact(
                session,
                owner,
                peer_id=r.peer_id,
                peer_kind=r.kind,
                is_bot=r.is_bot,
                display_name=r.name,
                username=r.username,
            )

    kb = InlineKeyboardBuilder()
    for r in results:
        marks = []
        if r.text_hits:
            marks.append(f"{r.text_hits} збіг.")
        if r.name_score:
            marks.append(f"ім'я {r.name_score}/5")
        meta = " · ".join(marks)
        label = f"{r.name}" + (f" · {meta}" if meta else "")
        if len(label) > 60:
            label = label[:57] + "…"
        kb.row(InlineKeyboardButton(text=label, callback_data=f"chat:{action}:{r.peer_id}"))
    kb.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="chat:cancel:0"))

    pretty_action = {
        "catchup": "«на чому зупинились»",
        "summary": "самарі",
        "tasks":   "задачі/обіцянки",
        "draft":   "чернетка відповіді",
    }.get(action, action)

    await message.answer(
        f"Знайшов підхожі чати. Обери — зберу {pretty_action}:",
        reply_markup=kb.as_markup(),
    )


async def _exec_set_setting(intent, message) -> None:
    key = (intent.get("key") or "").strip()
    value = intent.get("value")
    spec = SETTING_FIELDS.get(key)
    if spec is None:
        await message.answer(f"Не вмію змінювати «{key}».")
        return
    validated, err = _coerce_setting_value(spec, value)
    if err:
        await message.answer(f"Не зрозумів значення для <b>{key}</b>: {err}.")
        return
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        setattr(owner.settings, key, validated)
        new_tz = owner.settings.timezone
    if key == "timezone":
        await message.answer(f"✅ Часовий пояс: <b>{tz_short(new_tz)}</b>")
    elif isinstance(validated, bool):
        await message.answer(f"✅ <b>{key}</b>: {'УВІМК' if validated else 'ВИМК'}")
    else:
        shown = str(validated)
        if len(shown) > 100:
            shown = shown[:97] + "…"
        await message.answer(f"✅ <b>{key}</b> = <code>{shown}</code>")


async def _exec_add_news_topic(intent, message) -> None:
    topic = (intent.get("topic") or "").strip()
    if not topic:
        await message.answer("Не зрозумів, яку тему додати.")
        return
    try:
        hours = int(intent.get("hours") or 24)
    except (TypeError, ValueError):
        hours = 24
    hours = max(1, min(168, hours))
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        await add_news_topic(session, owner, topic, hours=hours)
    await message.answer(f"✅ Додав тему: <b>{topic}</b> (вікно {hours}год)")


async def _exec_remove_news_topic(intent, message) -> None:
    needle = (intent.get("topic") or "").strip().lower()
    if not needle:
        await message.answer("Яку тему видалити?")
        return
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        topics = await list_news_topics(session, owner)
        matched = [t for t in topics if needle in t.topic.lower()]
        if not matched:
            await message.answer(f"Тем за «{needle}» не знайшов.")
            return
        for t in matched:
            await delete_news_topic(session, owner, t.id)
    names = ", ".join(f"«{t.topic}»" for t in matched)
    await message.answer(f"🗑 Видалив: {names}")


def _render_labels_block(labels) -> str | None:
    if not labels:
        return None
    lines = []
    for l in labels:
        extra = f", повідомлення там починаються з «{l.message_prefix}»" if l.message_prefix else ""
        lines.append(f'- мітка "{l.label}" → чат «{l.peer_name}»{extra}')
    return "Мітки чатів власника (для label_chat/find_and_forward/log_to_label):\n" + "\n".join(lines)


def _quoted_text(message: Message) -> str | None:
    """Текст сообщения, на которое сделан реплай — вместе с его подписью/цитатой.

    Без этого фразы вида «распиши это как задачу» приходят в LLM без самой задачи.
    """
    src = message.reply_to_message
    if src is None:
        return None
    parts = [src.text, src.caption]
    quote = getattr(message, "quote", None)  # выделенный фрагмент цитаты, если есть
    if quote is not None and getattr(quote, "text", None):
        parts.insert(0, quote.text)
    body = "\n".join(p.strip() for p in parts if p and p.strip())
    return body or None


async def _process_text(
    raw: str,
    message: Message,
    state: FSMContext,
    userbot_manager: UserbotManager,
) -> None:
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        provider = await build_provider(session, owner)
        tz_name = owner.settings.timezone
        chat_labels = await list_chat_labels(session, owner)

    if provider is None:
        await message.answer(
            "Щоб я міг розуміти вільний текст — додай LLM-ключ у /settings → 🔑 API-ключі."
        )
        return

    now_local_str = now_in_tz(tz_name).strftime("%Y-%m-%d %H:%M")
    history_block = ctx_store.render_history_block(message.from_user.id)
    labels_block = _render_labels_block(chat_labels)
    try:
        intent = await route_intent(
            provider, raw,
            heavy=False,
            now_local=now_local_str,
            tz_name=tz_name,
            history_block=history_block,
            quoted_text=_quoted_text(message),
            labels_block=labels_block,
        )
    except Exception:
        logger.exception("agent route_intent failed")
        await message.answer("Не вдалося розібрати запит (LLM помилився).")
        return

    if intent.get("intent") == "multi":
        actions = intent.get("actions") or []
        if not isinstance(actions, list) or not actions:
            await message.answer("Не зрозумів, що зробити.")
            return
        for sub in actions:
            await _dispatch(sub, message, state, userbot_manager, tz_name=tz_name)
    else:
        await _dispatch(intent, message, state, userbot_manager, tz_name=tz_name)

    summary = _summarize_intent_for_memory(intent)
    ctx_store.add_turn(message.from_user.id, raw, summary)


def _summarize_intent_for_memory(intent: dict) -> str:
    # компактная запись «что я только что сделал» для памяти диалога
    kind = intent.get("intent")
    if kind == "multi":
        return "кілька дій: " + ", ".join(
            (a or {}).get("intent", "?") for a in intent.get("actions", [])[:5]
        )
    if kind == "send_message":
        return f"підготував надсилання «{(intent.get('text') or '')[:60]}» для {intent.get('recipient')}"
    if kind in {"summarize_chat", "tasks_for_chat", "draft_reply", "catchup"}:
        return f"{kind} з контактом {intent.get('contact')}"
    if kind == "find_in_chats":
        return f"шукав у чатах: {intent.get('query')}"
    if kind == "news_digest":
        return f"новини: {intent.get('topic')}"
    if kind == "set_setting":
        return f"налаштування {intent.get('key')} → {intent.get('value')}"
    if kind == "add_news_topic":
        return f"додав тему: {intent.get('topic')}"
    if kind == "remove_news_topic":
        return f"прибрав тему: {intent.get('topic')}"
    if kind == "add_reminder":
        return f"нагадування: {intent.get('text')}"
    if kind == "remove_reminder":
        return f"прибрав нагадування: {intent.get('query')}"
    if kind == "add_reminders_from_chat":
        return f"витягнув обіцянки з чату з {intent.get('contact')}"
    if kind == "list_todos":
        return "показав список обіцянок"
    if kind == "label_chat":
        return f"позначив чат міткою «{intent.get('label')}»"
    if kind == "find_and_forward":
        return f"шукав і пересилав повідомлення (мітка/чат: {intent.get('label') or intent.get('contact')})"
    if kind == "log_to_label":
        return f"дописував фактичне виконання в мітку «{intent.get('label')}»"
    if kind == "track_start":
        return f"почав таймер по проєкту «{intent.get('project')}»"
    if kind == "track_stop":
        return f"зупинив таймер по проєкту «{intent.get('project')}»" if intent.get("project") else "зупинив таймер"
    if kind == "list_active_tracks":
        return "показав активні запуски трекера"
    if kind == "track_report":
        return "показав звіт по трекеру за днями"
    if kind == "add_task":
        return f"додав завдання в проєкт «{intent.get('project')}»"
    if kind == "list_tasks":
        return "показав завдання за проєктами"
    if kind == "chat":
        return (intent.get("reply") or "")[:160]
    return kind or ""


@router.message(F.text & ~F.text.startswith("/"))
async def free_text(
    message: Message,
    state: FSMContext,
    userbot_manager: UserbotManager,
) -> None:
    if await state.get_state() is not None:
        return
    raw = (message.text or "").strip()
    if not raw:
        return
    await _process_text(raw, message, state, userbot_manager)


@router.message(F.voice | F.audio)
async def free_voice(
    message: Message,
    state: FSMContext,
    userbot_manager: UserbotManager,
) -> None:
    if await state.get_state() is not None:
        return

    media = message.voice or message.audio
    if media is None:
        return

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        mode = owner.settings.transcription_mode
        openai_key = await get_api_key(session, owner, "openai")

    media_dir = app_settings.data_dir / "media" / "control_bot"
    media_dir.mkdir(parents=True, exist_ok=True)
    target = media_dir / f"{message.message_id}_{media.file_unique_id}.ogg"

    notice = await message.answer("🎙 Слухаю… (транскрибую)")
    try:
        await message.bot.download(media.file_id, destination=str(target))
        text = await transcription_service.transcribe(
            target,
            file_id=media.file_unique_id,
            mode=mode,
            openai_key=openai_key,
        )
    except Exception:
        logger.exception("voice transcription failed")
        try:
            await notice.edit_text("❌ Не вдалося розпізнати голосове.")
        except Exception:
            pass
        return

    text = (text or "").strip()
    if not text:
        try:
            await notice.edit_text("Не почув тексту в цьому повідомленні.")
        except Exception:
            pass
        return

    try:
        await notice.edit_text(f"🎙 <i>Почув:</i> {text}")
    except Exception:
        pass

    await _process_text(text, message, state, userbot_manager)


async def _dispatch(intent, message, state, userbot_manager, *, tz_name: str) -> None:
    kind = intent.get("intent")
    if kind == "set_setting":
        await _exec_set_setting(intent, message)
        return
    if kind == "add_news_topic":
        await _exec_add_news_topic(intent, message)
        return
    if kind == "remove_news_topic":
        await _exec_remove_news_topic(intent, message)
        return
    if kind == "add_reminder":
        await _exec_add_reminder(intent, message, tz_name=tz_name)
        return
    if kind == "remove_reminder":
        await _exec_remove_reminder(intent, message)
        return
    if kind == "add_reminders_from_chat":
        await _exec_add_reminders_from_chat(intent, message, userbot_manager)
        return
    if kind == "label_chat":
        from src.bot.handlers.chat_labels import exec_label_chat
        await exec_label_chat(intent, message, state, userbot_manager)
        return
    if kind == "find_and_forward":
        from src.bot.handlers.chat_labels import exec_find_and_forward
        await exec_find_and_forward(intent, message, state, userbot_manager)
        return
    if kind == "log_to_label":
        from src.bot.handlers.chat_labels import exec_log_to_label
        await exec_log_to_label(intent, message, state, userbot_manager)
        return
    if kind == "track_start":
        from src.bot.handlers.track import exec_track_start
        await exec_track_start(intent, message, state, userbot_manager)
        return
    if kind == "track_stop":
        from src.bot.handlers.track import exec_track_stop
        await exec_track_stop(intent, message, state, userbot_manager)
        return
    if kind == "list_active_tracks":
        from src.bot.handlers.track import exec_list_active_tracks
        await exec_list_active_tracks(intent, message, state, userbot_manager)
        return
    if kind == "track_report":
        from src.bot.handlers.track import exec_track_report
        await exec_track_report(intent, message, state, userbot_manager)
        return
    if kind == "add_task":
        from src.bot.handlers.tasks import exec_task_add
        await exec_task_add(intent, message, state, userbot_manager)
        return
    if kind == "list_tasks":
        from src.bot.handlers.tasks import exec_list_tasks
        await exec_list_tasks(intent, message, state, userbot_manager)
        return
    await _execute_intent(intent, message, state, userbot_manager, tz_name=tz_name)


def _parse_iso_to_utc_naive(value):
    if not value:
        return None
    try:
        from datetime import datetime, timezone
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


async def _exec_add_reminder(intent, message, *, tz_name: str) -> None:
    text = (intent.get("text") or "").strip()
    if not text:
        await message.answer("Не зрозумів, про що нагадати. Уточни.")
        return
    when = _parse_iso_to_utc_naive(intent.get("when"))
    peer_query = (intent.get("peer_query") or "").strip()

    peer_id = 0
    peer_name = None
    if peer_query:
        from src.userbot.manager import _MANAGER_SINGLETON
        client = _MANAGER_SINGLETON.get_client(message.from_user.id) if _MANAGER_SINGLETON else None
        if client is not None:
            from src.core.contact_resolver import resolve
            async with get_session() as session:
                owner = await get_or_create_user(session, message.from_user.id)
            cands = await resolve(client, owner, peer_query)
            if cands:
                peer_id = cands[0].peer_id
                peer_name = cands[0].display_name

    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        await add_commitment(
            session,
            user_id=owner.id,
            peer_id=peer_id,
            peer_name=peer_name,
            message_id=None,
            direction="mine",
            text=text,
            deadline_at=when,
        )

    when_str = fmt_local(when, tz_name) if when else "без терміну"
    extra = f" (контакт: {peer_name})" if peer_name else ""
    note = "" if owner.settings.reminders_enabled else "\n\n⚠ Нагадування вимкнені — увімкни в /settings → ⏰."
    await message.answer(f"⏰ Нагадування додано: <b>{text}</b>\nКоли: {when_str}{extra}{note}")


async def _exec_remove_reminder(intent, message) -> None:
    needle = (intent.get("query") or "").strip().lower()
    if not needle:
        await message.answer("Яке нагадування прибрати?")
        return
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        items = await list_open_commitments(session, owner)
        matched = [
            c for c in items
            if needle in (c.text or "").lower()
            or (c.peer_name and needle in c.peer_name.lower())
        ]
        if not matched:
            await message.answer(f"Не знайшов нагадувань за «{needle}».")
            return
        for c in matched:
            await update_commitment_status(session, c.id, "cancelled")
    names = "\n".join(f"• {c.text}" for c in matched)
    await message.answer(f"🗑 Знято ({len(matched)}):\n{names}")


async def _exec_add_reminders_from_chat(intent, message, userbot_manager) -> None:
    contact_query = (intent.get("contact") or "").strip()
    if not contact_query:
        await message.answer("З яким контактом витягти обіцянки?")
        return
    client = userbot_manager.get_client(message.from_user.id)
    if client is None:
        await message.answer("Спершу /login.")
        return

    from src.core.contact_resolver import resolve
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        provider = await build_provider(session, owner)
    if provider is None:
        await message.answer("Потрібен LLM-ключ (/settings → 🔑).")
        return

    cands = await resolve(client, owner, contact_query)
    if not cands:
        await message.answer(f"Контакт «{contact_query}» не знайдено.")
        return
    target = cands[0]

    await message.answer(f"⏳ Підгружаю чат з <b>{target.label()}</b> і витягую обіцянки…")
    msgs = await load_chat(client, message.from_user.id, target.peer_id, limit=80, transcribe=True)
    async with get_session() as session:
        owner = await get_or_create_user(session, message.from_user.id)
        contact = await get_contact(session, owner, target.peer_id)
    items = await extract_and_save_commitments(
        provider, user_id=owner.id, contact=contact, messages=msgs
    )
    if not items:
        await message.answer("Явних обіцянок у цьому чаті не знайшов.")
        return
    lines = []
    for it in items:
        who = "Я" if it.get("direction") == "mine" else "Вони"
        deadline = it.get("deadline")
        tail = f" · до {deadline}" if deadline else ""
        lines.append(f"• <b>{who}</b>: {it.get('text', '')}{tail}")
    await message.answer(
        f"⏰ Поставив {len(items)} нагадувань із чату з {target.display_name}:\n\n"
        + "\n".join(lines)
    )
