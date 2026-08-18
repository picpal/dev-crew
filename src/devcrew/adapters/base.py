"""Adapter 계약(§6.2) — 6연산 Protocol + 테스트 대역 FakeAdapter."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Protocol

from ..schema import AgentInstance, Usage


class ResumeConfigMissingError(Exception):
    """resume()이 호출된 session_id에 이 adapter 인스턴스의 로컬 시작 설정 캐시가
    없을 때 발생 (예: cross-process 재시작 후 새 adapter 인스턴스에서 resume 호출).

    캐시 없이 resume하면 role system prompt/output schema/tool allowlist/
    can_use_tool/sandbox 등 시작 시점 enforcement가 SDK/provider 기본값으로
    조용히 폴백해 role 경계와 구조화 출력 강제를 우회할 수 있다 — 그래서 fail-closed
    한다. Cross-process 복구가 필요한 호출자는 resume(..., allow_unconfigured=True)
    로 의도적으로 이 검사를 건너뛸 수 있다 (registry 기반 cross-process 설정 복원은
    deferred B1, 이번 fix wave 범위 밖).
    """


@dataclass(frozen=True)
class TurnOutcome:
    text: str
    usage: Usage
    raw: dict = field(default_factory=dict)
    structured: dict | None = None


class ProviderAdapter(Protocol):
    async def start_session(self, inst: AgentInstance, initial_message: str, *,
                             system_prompt: str | None = None,
                             output_schema: dict | None = None,
                             mcp_servers: dict | None = None) -> str: ...
    async def send(self, session_id: str, message: str) -> TurnOutcome: ...
    async def resume(self, session_id: str, message: str) -> TurnOutcome: ...
    async def cancel(self, session_id: str) -> str: ...
    async def archive(self, session_id: str) -> str: ...
    async def get_usage(self, session_id: str) -> Usage: ...
    async def initial_usage(self, session_id: str) -> Usage | None: ...


class FakeAdapter:
    """스크립트된 응답을 돌려주는 대역. orchestration 로직 테스트 전용."""

    _ids = itertools.count(1)

    def __init__(self, script: list[str] | None = None, fail_after: int | None = None,
                 structured_script: list[dict] | None = None):
        self.script = list(script or [])
        self.fail_after = fail_after
        self.structured_script = list(structured_script or [])
        self.turns: dict[str, int] = {}
        self.last_system_prompt: str | None = None
        self.last_output_schema: dict | None = None
        self.last_mcp_servers: dict | None = None

    async def start_session(self, inst: AgentInstance, initial_message: str, *,
                             system_prompt: str | None = None,
                             output_schema: dict | None = None,
                             mcp_servers: dict | None = None) -> str:
        sid = f"fake-{next(self._ids)}"
        self.turns[sid] = 0
        self.last_system_prompt = system_prompt
        self.last_output_schema = output_schema
        self.last_mcp_servers = mcp_servers
        return sid

    async def send(self, session_id: str, message: str) -> TurnOutcome:
        if message is None:
            # 실 어댑터(Claude SDK/Codex SDK)는 None 프롬프트를 거부하고 크래시한다
            # (§Task4 R2 근본원인) — Fake도 같은 계약을 가져야 이 결함류를 unit이 잡는다.
            raise TypeError("FakeAdapter.send: message must not be None")
        n = self.turns[session_id]
        if self.fail_after is not None and n >= self.fail_after:
            raise RuntimeError("scripted failure")
        self.turns[session_id] = n + 1
        text = self.script[n] if n < len(self.script) else "done"
        structured = self.structured_script[n] if n < len(self.structured_script) else None
        return TurnOutcome(text=text, usage=Usage(output_tokens=1, raw={"fake": True}),
                           structured=structured)

    async def resume(self, session_id: str, message: str) -> TurnOutcome:
        return await self.send(session_id, message)

    async def cancel(self, session_id: str) -> str:
        return "CANCELLED"

    async def archive(self, session_id: str) -> str:
        return "ARCHIVED"

    async def get_usage(self, session_id: str) -> Usage:
        return Usage(output_tokens=self.turns.get(session_id, 0), raw={"fake": True})

    async def initial_usage(self, session_id: str) -> Usage | None:
        # FakeAdapter.start_session은(실 어댑터와 달리) 최초 메시지에 대해 turn을
        # 실행하지 않는다 — 버릴 usage 자체가 없으므로 항상 None (finding #6).
        return None
