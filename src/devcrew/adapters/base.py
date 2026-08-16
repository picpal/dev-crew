"""Adapter 계약(§6.2) — 6연산 Protocol + 테스트 대역 FakeAdapter."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Protocol

from ..schema import AgentInstance, Usage


@dataclass(frozen=True)
class TurnOutcome:
    text: str
    usage: Usage
    raw: dict = field(default_factory=dict)


class ProviderAdapter(Protocol):
    async def start_session(self, inst: AgentInstance, initial_message: str) -> str: ...
    async def send(self, session_id: str, message: str) -> TurnOutcome: ...
    async def resume(self, session_id: str, message: str) -> TurnOutcome: ...
    async def cancel(self, session_id: str) -> str: ...
    async def archive(self, session_id: str) -> str: ...
    async def get_usage(self, session_id: str) -> Usage: ...


class FakeAdapter:
    """스크립트된 응답을 돌려주는 대역. orchestration 로직 테스트 전용."""

    _ids = itertools.count(1)

    def __init__(self, script: list[str] | None = None, fail_after: int | None = None):
        self.script = list(script or [])
        self.fail_after = fail_after
        self.turns: dict[str, int] = {}

    async def start_session(self, inst: AgentInstance, initial_message: str) -> str:
        sid = f"fake-{next(self._ids)}"
        self.turns[sid] = 0
        return sid

    async def send(self, session_id: str, message: str) -> TurnOutcome:
        n = self.turns[session_id]
        if self.fail_after is not None and n >= self.fail_after:
            raise RuntimeError("scripted failure")
        self.turns[session_id] = n + 1
        text = self.script[n] if n < len(self.script) else "done"
        return TurnOutcome(text=text, usage=Usage(output_tokens=1, raw={"fake": True}))

    async def resume(self, session_id: str, message: str) -> TurnOutcome:
        return await self.send(session_id, message)

    async def cancel(self, session_id: str) -> str:
        return "CANCELLED"

    async def archive(self, session_id: str) -> str:
        return "ARCHIVED"

    async def get_usage(self, session_id: str) -> Usage:
        return Usage(output_tokens=self.turns.get(session_id, 0), raw={"fake": True})
