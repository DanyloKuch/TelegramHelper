from telethon import TelegramClient
from telethon.tl.custom import Message as TgMessage


async def find_message(
    client: TelegramClient,
    peer_id: int,
    prefix: str | None = None,
    *,
    limit: int = 200,
) -> TgMessage | None:
    """Найновіше текстове повідомлення в чаті peer_id, з будь-яким автором.

    Навмисно НЕ фільтрує за from_user="me": повідомлення може бути від співрозмовника
    (напр. тренер надсилає план тренувань власнику особисто, а не власник сам собі), і
    фільтр по "я" пропускав би саме ті повідомлення, які власник хотів знайти.

    Порівнюємо по .raw_text, а не .text: Telethon .text реконструює форматування назад у
    markdown (напр. Bold-сутність стає "**Тиждень 14/2**"), і startswith("тиждень") ламався
    б об оці зайві "**" на початку. .raw_text — це необроблений текст без такої декорації.

    Якщо задано prefix — шукає найновіше повідомлення з таким початком тексту
    (регістронезалежно); iter_messages віддає від найновіших до найстаріших, тому перший
    збіг — актуальне повідомлення (напр. "Тиждень N" цього тижня).
    """
    entity = await client.get_entity(peer_id)
    needle = prefix.strip().casefold() if prefix else None
    async for msg in client.iter_messages(entity, limit=limit):
        text = (msg.raw_text or msg.message or "").strip()
        if not text:
            continue
        if needle is None or text.casefold().startswith(needle):
            return msg
    return None
