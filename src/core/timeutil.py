"""Часовой пояс пользователя. В БД храним naive UTC, при показе и сравнении
с расписаниями (digest_time, news_digest_time) переводим в TZ владельца."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# популярные пресеты для быстрых кнопок
TZ_PRESETS: list[str] = [
    "UTC",
    "Europe/Moscow",
    "Europe/Kiev",
    "Europe/Warsaw",
    "Europe/London",
    "Europe/Berlin",
    "Asia/Almaty",
    "Asia/Tbilisi",
    "Asia/Dubai",
    "Asia/Tokyo",
    "America/New_York",
    "America/Los_Angeles",
]


def parse_tz(name: str | None) -> ZoneInfo:
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def is_valid_tz(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def now_in_tz(tz_name: str | None) -> datetime:
    return datetime.now(parse_tz(tz_name))


def utc_to_local(dt: datetime, tz_name: str | None) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(parse_tz(tz_name))


def fmt_local(dt: datetime | None, tz_name: str | None, *, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if dt is None:
        return "—"
    local = utc_to_local(dt, tz_name)
    return local.strftime(fmt)


def fmt_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}г {m:02d}хв"
    if m:
        return f"{m}хв"
    return f"{s}с"


def start_of_local_day_utc(tz_name: str | None, days_ago: int = 0) -> datetime:
    """Північ (00:00) у TZ юзера N днів тому, повернута як naive UTC — зручно як нижня межа для запитів."""
    local_midnight = now_in_tz(tz_name).replace(hour=0, minute=0, second=0, microsecond=0)
    local_midnight -= timedelta(days=days_ago)
    return local_midnight.astimezone(timezone.utc).replace(tzinfo=None)


def tz_short(tz_name: str | None) -> str:
    tz = parse_tz(tz_name)
    offset = datetime.now(tz).utcoffset()
    if offset is None:
        return tz.key
    total_min = int(offset.total_seconds() // 60)
    sign = "+" if total_min >= 0 else "-"
    h, m = divmod(abs(total_min), 60)
    suffix = f"UTC{sign}{h}" + (f":{m:02d}" if m else "")
    return f"{tz.key} ({suffix})"
