from rapidfuzz import fuzz, process
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ChatLabel, User
from src.db.repo import list_chat_labels


async def resolve_label(
    session: AsyncSession,
    user: User,
    query: str,
    *,
    min_score: int = 60,
) -> ChatLabel | None:
    """Находит метку чата владельца по свободной формулировке («моя тренировка», «тренування»).

    Точное/подстрочное совпадение — приоритет; при единственной метке считаем её угаданной
    даже при слабом совпадении (частый случай, когда владелец настроил только одну метку).
    """
    query = (query or "").strip().casefold()
    if not query:
        return None
    labels = await list_chat_labels(session, user)
    if not labels:
        return None

    for l in labels:
        if l.label == query:
            return l
    for l in labels:
        if query in l.label or l.label in query:
            return l
    if len(labels) == 1:
        return labels[0]

    choices = {l.id: l.label for l in labels}
    match = process.extractOne(query, choices, scorer=fuzz.WRatio)
    if match and match[1] >= min_score:
        by_id = {l.id: l for l in labels}
        return by_id[match[2]]
    return None
