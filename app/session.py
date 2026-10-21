"""Session state - the single owner of everything the server remembers.

The 2-round clarifying limit lives here, on the server side, so a modified
frontend cannot ask a third question. State is in memory only; persisting it
is the (optional) memory module's job.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

MAX_TURNS = 20


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    text: str
    at: float


@dataclass
class Session:
    id: str
    created_at: float
    last_seen: float
    clarify_count: int = 0
    pending: dict[str, Any] | None = None
    context: dict[str, Any] = field(default_factory=dict)
    turns: list[Turn] = field(default_factory=list)

    def add_turn(self, role: str, text: str) -> None:
        self.turns.append(Turn(role=role, text=text[:1000], at=time.time()))
        del self.turns[:-MAX_TURNS]

    def remember(self, **values: Any) -> None:
        self.context.update({key: value for key, value in values.items() if value is not None})

    def answers(self) -> list[str]:
        return list(self.context.get("answers") or [])

    def add_answer(self, feature_id: str) -> None:
        answers = self.answers()
        answers.append(feature_id)
        self.context["answers"] = answers


class SessionStore:
    def __init__(self, ttl_seconds: int = 1800, max_clarify_rounds: int = 2, clock=time.time) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_clarify_rounds = max_clarify_rounds
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ access

    def get_or_create(self, session_id: str | None = None) -> Session:
        with self._lock:
            self._prune_locked()
            now = self._clock()
            if session_id:
                existing = self._sessions.get(session_id)
                if existing is not None:
                    existing.last_seen = now
                    return existing
            new_id = session_id or uuid.uuid4().hex
            session = Session(id=new_id, created_at=now, last_seen=now)
            self._sessions[new_id] = session
            return session

    def get(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        with self._lock:
            self._prune_locked()
            session = self._sessions.get(session_id)
            if session is not None:
                session.last_seen = self._clock()
            return session

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    # ------------------------------------------------------------ clarify budget

    def can_ask(self, session: Session) -> bool:
        return session.clarify_count < self.max_clarify_rounds

    def note_question(self, session: Session, payload: dict[str, Any]) -> None:
        session.clarify_count += 1
        session.pending = payload

    def clear_pending(self, session: Session) -> None:
        session.pending = None

    # ------------------------------------------------------------ housekeeping

    def _prune_locked(self) -> None:
        cutoff = self._clock() - self.ttl_seconds
        stale = [key for key, session in self._sessions.items() if session.last_seen < cutoff]
        for key in stale:
            del self._sessions[key]
