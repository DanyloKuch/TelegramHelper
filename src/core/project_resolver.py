from rapidfuzz import fuzz, process
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Project, User
from src.db.repo import list_projects


async def resolve_project(
    session: AsyncSession,
    user: User,
    query: str,
    *,
    min_score: int = 70,
    assume_single: bool = False,
    candidates: list[Project] | None = None,
) -> Project | None:
    """Находит існуючий проєкт за вільною назвою.

    candidates=None → шукає серед УСІХ проєктів власника (дедуп при старті нового запуску,
    щоб "телеграм хелпер" не наплодив дубль поруч із "TelegramHelper").
    candidates=<список> → шукає лише серед переданих (напр. проєктів з активними запусками,
    коли зупиняємо таймер).

    assume_single=True — якщо кандидат лише один, вважаємо, що мали на увазі саме його,
    навіть при слабкому текстовому збігу. Доречно для "зупини таймер" (один активний — і без
    точної назви зрозуміло, що зупиняти), але НЕБЕЗПЕЧНО для старту нового проєкту: там це
    примусово зматчило б випадкову нову назву з єдиним існуючим проєктом. Тому за замовчуванням
    вимкнено.
    """
    query = (query or "").strip().casefold()
    if not query:
        return None
    projects = candidates if candidates is not None else await list_projects(session, user)
    if not projects:
        return None

    for p in projects:
        if p.name.casefold() == query:
            return p
    for p in projects:
        pl = p.name.casefold()
        if query in pl or pl in query:
            return p
    if assume_single and len(projects) == 1:
        return projects[0]

    choices = {p.id: p.name for p in projects}
    match = process.extractOne(query, choices, scorer=fuzz.WRatio)
    if match and match[1] >= min_score:
        by_id = {p.id: p for p in projects}
        return by_id[match[2]]
    return None
