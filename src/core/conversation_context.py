"""Краткосрочная память диалога с control-bot: последние N ходов и последний
обсуждаемый контакт. Нужно чтобы «напиши ему», «в том же чате» правильно
резолвились без повторения имени. In-memory, переживать рестарт не должно."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from time import time


MAX_TURNS = 10
LAST_PEER_TTL_SECONDS = 30 * 60  # 30 минут


@dataclass
class _Ctx:
    turns: deque = field(default_factory=lambda: deque(maxlen=MAX_TURNS))
    last_peer_id: int | None = None
    last_peer_name: str | None = None
    last_peer_at: float = 0.0


_STORE: dict[int, _Ctx] = {}


def _get(user_id: int) -> _Ctx:
    ctx = _STORE.get(user_id)
    if ctx is None:
        ctx = _Ctx()
        _STORE[user_id] = ctx
    return ctx


def add_turn(user_id: int, user_text: str, assistant_summary: str) -> None:
    ctx = _get(user_id)
    user_text = (user_text or "").strip()
    assistant_summary = (assistant_summary or "").strip()
    if not user_text and not assistant_summary:
        return
    ctx.turns.append((user_text[:900], assistant_summary[:400]))


def set_last_peer(user_id: int, peer_id: int, peer_name: str | None) -> None:
    ctx = _get(user_id)
    ctx.last_peer_id = peer_id
    ctx.last_peer_name = peer_name
    ctx.last_peer_at = time()


def get_last_peer(user_id: int) -> tuple[int, str | None] | None:
    ctx = _get(user_id)
    if ctx.last_peer_id is None:
        return None
    if (time() - ctx.last_peer_at) > LAST_PEER_TTL_SECONDS:
        return None
    return ctx.last_peer_id, ctx.last_peer_name


def get_recent_turns(user_id: int) -> list[tuple[str, str]]:
    return list(_get(user_id).turns)


def render_history_block(user_id: int) -> str:
    parts: list[str] = []
    last = get_last_peer(user_id)
    if last is not None:
        peer_id, name = last
        label = name or str(peer_id)
        parts.append(f"Последний упомянутый контакт: {label} (peer_id={peer_id}). "
                     f"Если фраза вроде «ему», «ей», «в том же чате», «там» — "
                     f"подставляй именно его.")

    turns = get_recent_turns(user_id)
    if turns:
        lines = ["Недавний диалог с владельцем (для понимания «то/там/ему»):"]
        for u, a in turns[-MAX_TURNS:]:
            if u:
                lines.append(f"  Владелец: {u}")
            if a:
                lines.append(f"  Я ответил: {a}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
