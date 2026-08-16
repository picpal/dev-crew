"""Codex Adapter (#11) — AsyncCodex 단일 인스턴스 공유, 6연산 전부 네이티브."""
from __future__ import annotations

from openai_codex import ApprovalMode, AsyncCodex, Sandbox

from ..enforcement import codex_session_kwargs
from ..schema import AgentInstance, Usage
from ..store.registry import SessionRegistry
from ..store.trace import TraceStore
from .base import TurnOutcome


def _usage_from_turn(result) -> Usage:
    total = result.usage.total if result.usage else None
    return Usage(
        input_tokens=getattr(total, "input_tokens", None),
        output_tokens=getattr(total, "output_tokens", None),
        cached_input_tokens=getattr(total, "cached_input_tokens", None),
        reasoning_output_tokens=getattr(total, "reasoning_output_tokens", None),
        duration_ms=result.duration_ms,
        raw={"status": str(result.status),
             "usage": total.model_dump() if total is not None else None},
    )


class CodexAdapter:
    def __init__(self, trace: TraceStore, registry: SessionRegistry):
        self.trace = trace
        self.registry = registry
        self._codex: AsyncCodex | None = None
        self._threads: dict[str, object] = {}      # thread_id -> AsyncThread
        self._efforts: dict[str, str] = {}

    async def _client(self) -> AsyncCodex:
        if self._codex is None:
            self._codex = AsyncCodex()
            await self._codex.__aenter__()
        return self._codex

    async def start_session(self, inst: AgentInstance, initial_message: str) -> str:
        codex = await self._client()
        kw = codex_session_kwargs(inst.role, cwd=inst.worktree)
        thread = await codex.thread_start(
            model=inst.model,
            sandbox=Sandbox[kw["sandbox_name"]],
            cwd=kw["cwd"],
            approval_mode=ApprovalMode[kw["approval_mode_name"]],
        )
        self._threads[thread.id] = thread
        self._efforts[thread.id] = inst.effort_level.value.lower()
        await self.send(thread.id, initial_message)
        return thread.id

    async def send(self, session_id: str, message: str) -> TurnOutcome:
        thread = self._threads[session_id]
        result = await thread.run(message, effort=self._efforts.get(session_id))
        return TurnOutcome(text=result.final_response or "",
                           usage=_usage_from_turn(result),
                           raw={"turn_id": result.id})

    async def resume(self, session_id: str, message: str) -> TurnOutcome:
        codex = await self._client()
        resumed = await codex.thread_resume(session_id)
        self._threads[session_id] = resumed
        return await self.send(session_id, message)

    async def cancel(self, session_id: str) -> str:
        thread = self._threads[session_id]
        handle = await thread.turn("cancel target")     # POC: turn 시작 후 즉시 중단
        await handle.interrupt()
        return "CANCELLED"

    async def archive(self, session_id: str) -> str:
        codex = await self._client()
        await codex.thread_archive(session_id)
        return "ARCHIVED"

    async def get_usage(self, session_id: str) -> Usage:
        raise NotImplementedError("usage는 각 TurnOutcome.usage로 수집한다")

    async def thread_exists(self, thread_id: str) -> bool:
        codex = await self._client()
        page = await codex.thread_list()
        # ThreadListResponse.data (실제 필드명; brief 초안의 `items`는 openai-codex
        # 0.144.4에 존재하지 않아 항상 빈 리스트로 평가됨 — Step 1 검증으로 확인).
        return any(t.id == thread_id for t in getattr(page, "data", []) or [])
