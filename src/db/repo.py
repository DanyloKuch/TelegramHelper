import asyncio
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text as sql_text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.crypto import decrypt, encrypt
from src.db.models import (
    ApiKey,
    AutoReplyLog,
    ChatLabel,
    Commitment,
    Contact,
    Message,
    NewsTopic,
    PendingAction,
    Project,
    TelegramSession,
    TimeEntry,
    TranscriptionCache,
    User,
    UserSettings,
)


# Фоновые таски (digest, news, reminders, auto_sync) одновременно тыкаются в
# get_or_create_user на пустой БД → UNIQUE race. Сериализуем процесс-локом.
_user_lock = asyncio.Lock()


async def get_or_create_user(session: AsyncSession, telegram_id: int) -> User:
    async with _user_lock:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(telegram_id=telegram_id, settings=UserSettings())
            session.add(user)
            await session.flush()
        elif user.settings is None:
            user.settings = UserSettings(user_id=user.id)
            session.add(user.settings)
            await session.flush()
        return user


async def save_telegram_session(
    session: AsyncSession,
    user: User,
    *,
    api_id: int,
    api_hash: str,
    session_string: str,
    phone: str,
    account_label: str | None,
) -> None:
    payload = TelegramSession(
        user_id=user.id,
        api_id=api_id,
        api_hash_enc=encrypt(api_hash),
        session_string_enc=encrypt(session_string),
        phone=phone,
        account_label=account_label,
    )
    existing = await session.get(TelegramSession, user.id)
    if existing is not None:
        await session.delete(existing)
        await session.flush()
    session.add(payload)


async def load_telegram_session(session: AsyncSession, user: User) -> tuple[int, str, str] | None:
    row = await session.get(TelegramSession, user.id)
    if row is None:
        return None
    return row.api_id, decrypt(row.api_hash_enc), decrypt(row.session_string_enc)


async def delete_telegram_session(session: AsyncSession, user: User) -> None:
    row = await session.get(TelegramSession, user.id)
    if row is not None:
        await session.delete(row)


async def upsert_api_key(session: AsyncSession, user: User, provider: str, key: str) -> None:
    result = await session.execute(
        select(ApiKey).where(ApiKey.user_id == user.id, ApiKey.provider == provider)
    )
    existing = result.scalar_one_or_none()
    enc = encrypt(key)
    if existing is None:
        session.add(ApiKey(user_id=user.id, provider=provider, key_enc=enc))
    else:
        existing.key_enc = enc


async def get_api_key(session: AsyncSession, user: User, provider: str) -> str | None:
    result = await session.execute(
        select(ApiKey).where(ApiKey.user_id == user.id, ApiKey.provider == provider)
    )
    row = result.scalar_one_or_none()
    return decrypt(row.key_enc) if row is not None else None


async def upsert_contact(
    session: AsyncSession,
    user: User,
    *,
    peer_id: int,
    peer_kind: str,
    display_name: str,
    username: str | None = None,
    phone: str | None = None,
    is_bot: bool = False,
    is_archived: bool | None = None,
) -> Contact:
    result = await session.execute(
        select(Contact).where(Contact.user_id == user.id, Contact.peer_id == peer_id)
    )
    contact = result.scalar_one_or_none()
    if contact is None:
        contact = Contact(
            user_id=user.id,
            peer_id=peer_id,
            peer_kind=peer_kind,
            is_bot=is_bot,
            is_archived=bool(is_archived) if is_archived is not None else False,
            display_name=display_name,
            username=username,
            phone=phone,
        )
        session.add(contact)
        await session.flush()
    else:
        contact.peer_kind = peer_kind
        contact.is_bot = is_bot
        if is_archived is not None:
            contact.is_archived = is_archived
        contact.display_name = display_name
        contact.username = username
        contact.phone = phone
    return contact


async def list_contacts(
    session: AsyncSession,
    user: User,
    *,
    kinds: tuple[str, ...] | None = None,
    include_bots: bool = False,
    only_news_sources: bool = False,
    include_archived: bool | None = None,
) -> list[Contact]:
    # include_archived=None → берём решение из настроек пользователя
    if include_archived is None:
        include_archived = not user.settings.ignore_archived if user.settings else False

    query = select(Contact).where(Contact.user_id == user.id)
    if kinds:
        query = query.where(Contact.peer_kind.in_(kinds))
    if not include_bots:
        query = query.where(Contact.is_bot.is_(False))
    if only_news_sources:
        query = query.where(Contact.is_news_source.is_(True))
    if not include_archived:
        query = query.where(Contact.is_archived.is_(False))
    result = await session.execute(query)
    return list(result.scalars().all())


async def set_news_source(session: AsyncSession, user: User, peer_id: int, value: bool) -> bool:
    result = await session.execute(
        select(Contact).where(Contact.user_id == user.id, Contact.peer_id == peer_id)
    )
    contact = result.scalar_one_or_none()
    if contact is None:
        return False
    contact.is_news_source = value
    return True


async def get_contact(session: AsyncSession, user: User, peer_id: int) -> Contact | None:
    result = await session.execute(
        select(Contact).where(Contact.user_id == user.id, Contact.peer_id == peer_id)
    )
    return result.scalar_one_or_none()


async def upsert_message(
    session: AsyncSession,
    *,
    user_id: int,
    peer_id: int,
    message_id: int,
    sender_id: int | None,
    sender_name: str | None,
    is_outgoing: bool,
    date: datetime,
    kind: str,
    text: str | None,
    transcript: str | None = None,
    media_path: str | None = None,
    extracted_text: str | None = None,
) -> None:
    stmt = sqlite_insert(Message).values(
        user_id=user_id,
        peer_id=peer_id,
        message_id=message_id,
        sender_id=sender_id,
        sender_name=sender_name,
        is_outgoing=is_outgoing,
        date=date,
        kind=kind,
        text=text,
        transcript=transcript,
        media_path=media_path,
        extracted_text=extracted_text,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "peer_id", "message_id"],
        set_={
            "text": stmt.excluded.text,
            "transcript": stmt.excluded.transcript,
            "extracted_text": stmt.excluded.extracted_text,
            "media_path": stmt.excluded.media_path,
            "kind": stmt.excluded.kind,
            "sender_name": stmt.excluded.sender_name,
        },
    )
    await session.execute(stmt)


async def fetch_chat_messages(
    session: AsyncSession,
    user: User,
    peer_id: int,
    limit: int = 50,
) -> list[Message]:
    result = await session.execute(
        select(Message)
        .where(Message.user_id == user.id, Message.peer_id == peer_id)
        .order_by(Message.date.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))


@dataclass
class FtsHit:
    user_id: int
    peer_id: int
    message_id: int
    sender_name: str | None
    snippet: str
    rank: float


def _fts_query_for(query: str) -> str:
    # каждое слово → prefix-match, склейка через OR. Это толерантнее MATCH'а целой фразы.
    parts = []
    for raw in query.split():
        clean = "".join(ch for ch in raw if ch.isalnum() or ch in "_-")
        if len(clean) >= 2:
            parts.append(clean.lower() + "*")
    if not parts:
        return ""
    return " OR ".join(parts)


async def fts_search(
    session: AsyncSession,
    user_id: int,
    query: str,
    *,
    limit: int = 50,
) -> list[FtsHit]:
    fts_q = _fts_query_for(query)
    if not fts_q:
        return []
    sql = """
        SELECT m.user_id, m.peer_id, m.message_id, m.sender_name,
               snippet(messages_fts, -1, '', '', '…', 16) AS snippet,
               bm25(messages_fts) AS rank
        FROM messages_fts
        JOIN messages m ON m.id = messages_fts.rowid
        WHERE messages_fts MATCH :q AND m.user_id = :uid
        ORDER BY rank
        LIMIT :lim
    """
    result = await session.execute(
        sql_text(sql),
        {"q": fts_q, "uid": user_id, "lim": limit},
    )
    rows = result.mappings().all()
    return [
        FtsHit(
            user_id=int(r["user_id"]),
            peer_id=int(r["peer_id"]),
            message_id=int(r["message_id"]),
            sender_name=r["sender_name"],
            snippet=r["snippet"] or "",
            rank=float(r["rank"]) if r["rank"] is not None else 0.0,
        )
        for r in rows
    ]


async def fetch_my_messages_in_chat(
    session: AsyncSession,
    user: User,
    peer_id: int,
    limit: int = 100,
) -> list[Message]:
    result = await session.execute(
        select(Message)
        .where(
            Message.user_id == user.id,
            Message.peer_id == peer_id,
            Message.is_outgoing.is_(True),
        )
        .order_by(Message.date.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))


async def get_cached_transcript(session: AsyncSession, file_id: str) -> str | None:
    row = await session.get(TranscriptionCache, file_id)
    return row.text if row else None


async def cache_transcript(
    session: AsyncSession,
    file_id: str,
    text: str,
    duration_seconds: float | None = None,
) -> None:
    existing = await session.get(TranscriptionCache, file_id)
    if existing is None:
        session.add(TranscriptionCache(file_id=file_id, text=text, duration_seconds=duration_seconds))
    else:
        existing.text = text
        existing.duration_seconds = duration_seconds


def _norm_commitment_text(value: str) -> str:
    return " ".join(value.split()).casefold().rstrip(".!…")


async def add_commitment(
    session: AsyncSession,
    *,
    user_id: int,
    peer_id: int,
    peer_name: str | None,
    message_id: int | None,
    direction: str,
    text: str,
    deadline_at: datetime | None,
) -> Commitment:
    """Идемпотентно: одно и то же обещание прилетает повторно.

    Анализ чата (/chat, /catchup) переизвлекает обещания при каждом прогоне, а
    путь «напомни мне» сохраняет их же с peer_id=0, когда контакт не распознан.
    Поэтому ключ дедупа — текст + направление, а peer_id=0 считаем «любым».
    """
    norm = _norm_commitment_text(text)
    result = await session.execute(
        select(Commitment).where(
            Commitment.user_id == user_id,
            Commitment.direction == direction,
            Commitment.status == "open",
        )
    )
    for existing in result.scalars().all():
        if _norm_commitment_text(existing.text) != norm:
            continue
        # Разные собеседники с одинаковой формулировкой — это разные обещания.
        if existing.peer_id != peer_id and existing.peer_id != 0 and peer_id != 0:
            continue
        # Дополняем недостающее, но не перетираем уже известное:
        # повторный прогон LLM любит подставить «конец дня» вместо реального срока.
        if not existing.peer_id and peer_id:
            existing.peer_id = peer_id
            existing.peer_name = peer_name
        if existing.deadline_at is None and deadline_at is not None:
            existing.deadline_at = deadline_at
        if existing.message_id is None and message_id is not None:
            existing.message_id = message_id
        await session.flush()
        return existing

    c = Commitment(
        user_id=user_id,
        peer_id=peer_id,
        peer_name=peer_name,
        message_id=message_id,
        direction=direction,
        text=text,
        deadline_at=deadline_at,
    )
    session.add(c)
    await session.flush()
    return c


async def list_open_commitments(
    session: AsyncSession,
    user: User,
    *,
    direction: str | None = None,
) -> list[Commitment]:
    query = select(Commitment).where(
        Commitment.user_id == user.id,
        Commitment.status == "open",
    )
    if direction:
        query = query.where(Commitment.direction == direction)
    query = query.order_by(Commitment.deadline_at.is_(None), Commitment.deadline_at.asc())
    result = await session.execute(query)
    return list(result.scalars().all())


async def update_commitment_status(session: AsyncSession, commitment_id: int, status: str) -> None:
    c = await session.get(Commitment, commitment_id)
    if c is not None:
        c.status = status


async def add_auto_reply_log(
    session: AsyncSession,
    *,
    user_id: int,
    peer_id: int,
    peer_name: str | None,
    incoming_text: str | None,
    reply_text: str,
) -> None:
    session.add(
        AutoReplyLog(
            user_id=user_id,
            peer_id=peer_id,
            peer_name=peer_name,
            incoming_text=incoming_text,
            reply_text=reply_text,
        )
    )


async def list_recent_auto_replies(
    session: AsyncSession,
    user: User,
    *,
    limit: int = 10,
) -> list[AutoReplyLog]:
    result = await session.execute(
        select(AutoReplyLog)
        .where(AutoReplyLog.user_id == user.id)
        .order_by(AutoReplyLog.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def create_pending_action(
    session: AsyncSession,
    *,
    user_id: int,
    kind: str,
    payload: str,
) -> PendingAction:
    pa = PendingAction(user_id=user_id, kind=kind, payload=payload)
    session.add(pa)
    await session.flush()
    return pa


async def get_pending_action(session: AsyncSession, action_id: int) -> PendingAction | None:
    return await session.get(PendingAction, action_id)


async def delete_pending_action(session: AsyncSession, action_id: int) -> None:
    pa = await session.get(PendingAction, action_id)
    if pa is not None:
        await session.delete(pa)


async def list_news_topics(
    session: AsyncSession,
    user: User,
    *,
    only_enabled: bool = False,
) -> list[NewsTopic]:
    query = select(NewsTopic).where(NewsTopic.user_id == user.id).order_by(NewsTopic.created_at.asc())
    if only_enabled:
        query = query.where(NewsTopic.enabled.is_(True))
    result = await session.execute(query)
    return list(result.scalars().all())


async def add_news_topic(
    session: AsyncSession,
    user: User,
    topic: str,
    *,
    hours: int = 24,
) -> NewsTopic:
    nt = NewsTopic(user_id=user.id, topic=topic.strip(), hours=hours)
    session.add(nt)
    await session.flush()
    return nt


async def delete_news_topic(session: AsyncSession, user: User, topic_id: int) -> bool:
    nt = await session.get(NewsTopic, topic_id)
    if nt is None or nt.user_id != user.id:
        return False
    await session.delete(nt)
    return True


async def toggle_news_topic(session: AsyncSession, user: User, topic_id: int) -> bool | None:
    nt = await session.get(NewsTopic, topic_id)
    if nt is None or nt.user_id != user.id:
        return None
    nt.enabled = not nt.enabled
    return nt.enabled


async def upsert_chat_label(
    session: AsyncSession,
    user: User,
    *,
    peer_id: int,
    peer_name: str,
    label: str,
    message_prefix: str | None = None,
) -> ChatLabel:
    norm = label.strip().casefold()
    result = await session.execute(
        select(ChatLabel).where(ChatLabel.user_id == user.id, ChatLabel.label == norm)
    )
    existing = result.scalar_one_or_none()
    if existing is None:
        existing = ChatLabel(
            user_id=user.id,
            peer_id=peer_id,
            peer_name=peer_name,
            label=norm,
            message_prefix=message_prefix,
        )
        session.add(existing)
        await session.flush()
    else:
        existing.peer_id = peer_id
        existing.peer_name = peer_name
        if message_prefix is not None:
            existing.message_prefix = message_prefix
    return existing


async def list_chat_labels(session: AsyncSession, user: User) -> list[ChatLabel]:
    result = await session.execute(
        select(ChatLabel).where(ChatLabel.user_id == user.id).order_by(ChatLabel.label.asc())
    )
    return list(result.scalars().all())


async def get_chat_label(session: AsyncSession, user: User, label: str) -> ChatLabel | None:
    norm = label.strip().casefold()
    result = await session.execute(
        select(ChatLabel).where(ChatLabel.user_id == user.id, ChatLabel.label == norm)
    )
    return result.scalar_one_or_none()


async def delete_chat_label(session: AsyncSession, user: User, label: str) -> bool:
    existing = await get_chat_label(session, user, label)
    if existing is None:
        return False
    await session.delete(existing)
    return True


async def get_or_create_project(session: AsyncSession, user: User, name: str) -> Project:
    name = name.strip()
    result = await session.execute(
        select(Project).where(Project.user_id == user.id, Project.name == name)
    )
    project = result.scalar_one_or_none()
    if project is None:
        project = Project(user_id=user.id, name=name)
        session.add(project)
        await session.flush()
    return project


async def get_project(session: AsyncSession, project_id: int) -> Project | None:
    return await session.get(Project, project_id)


async def list_projects(session: AsyncSession, user: User) -> list[Project]:
    result = await session.execute(
        select(Project).where(Project.user_id == user.id).order_by(Project.created_at.desc())
    )
    return list(result.scalars().all())


async def start_time_entry(
    session: AsyncSession,
    *,
    user_id: int,
    project_id: int,
    note: str | None = None,
) -> TimeEntry:
    entry = TimeEntry(user_id=user_id, project_id=project_id, note=note)
    session.add(entry)
    await session.flush()
    return entry


async def stop_time_entry(session: AsyncSession, entry_id: int, *, ended_at: datetime) -> TimeEntry | None:
    entry = await session.get(TimeEntry, entry_id)
    if entry is None or entry.ended_at is not None:
        return None
    entry.ended_at = ended_at
    return entry


async def list_active_entries(session: AsyncSession, user: User) -> list[TimeEntry]:
    result = await session.execute(
        select(TimeEntry)
        .where(TimeEntry.user_id == user.id, TimeEntry.ended_at.is_(None))
        .order_by(TimeEntry.started_at.asc())
    )
    return list(result.scalars().all())
