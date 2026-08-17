"""Codex Adapter (#11) — AsyncCodex 단일 인스턴스 공유, 6연산 전부 네이티브."""
from __future__ import annotations

import json

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
        self._schemas: dict[str, dict | None] = {}  # thread_id -> output_schema, for send()
        # thread_id -> {sandbox_name, cwd, approval_mode_name, effort, output_schema}:
        # cached at start_session so resume() can re-pass the role's enforcement pin
        # instead of falling back to thread_resume()'s own defaults (finding #2), and
        # can restore the structured-output schema so post-resume turns keep forcing
        # schema-conformant output (system_prompt/base_instructions is NOT restored
        # here — same deferred gap B1 as claude_code.py's resume()).
        self._session_config: dict[str, dict] = {}

    async def _client(self) -> AsyncCodex:
        if self._codex is None:
            self._codex = AsyncCodex()
            await self._codex.__aenter__()
        return self._codex

    async def start_session(self, inst: AgentInstance, initial_message: str, *,
                             system_prompt: str | None = None,
                             output_schema: dict | None = None) -> str:
        codex = await self._client()
        kw = codex_session_kwargs(inst.role, cwd=inst.worktree)
        thread = await codex.thread_start(
            model=inst.model,
            sandbox=Sandbox[kw["sandbox_name"]],
            cwd=kw["cwd"],
            approval_mode=ApprovalMode[kw["approval_mode_name"]],
            base_instructions=system_prompt,
        )
        self._threads[thread.id] = thread
        effort = inst.effort_level.value.lower()
        self._efforts[thread.id] = effort
        self._schemas[thread.id] = output_schema
        self._session_config[thread.id] = {
            "sandbox_name": kw["sandbox_name"],
            "cwd": kw["cwd"],
            "approval_mode_name": kw["approval_mode_name"],
            "effort": effort,
            "output_schema": output_schema,
        }
        await self.send(thread.id, initial_message)
        return thread.id

    async def send(self, session_id: str, message: str) -> TurnOutcome:
        thread = self._threads[session_id]
        schema = self._schemas.get(session_id)
        result = await thread.run(message, effort=self._efforts.get(session_id),
                                   output_schema=schema)
        structured = None
        if schema and result.final_response:
            try:
                structured = json.loads(result.final_response)
            except json.JSONDecodeError:
                structured = None    # provider가 스키마 강제하므로 정상 경로에선 발생 안 함
        return TurnOutcome(text=result.final_response or "",
                           usage=_usage_from_turn(result),
                           raw={"turn_id": result.id},
                           structured=structured)

    async def resume(self, session_id: str, message: str) -> TurnOutcome:
        """archive/disconnect 이후 thread를 재개한다 (#11).

        thread_resume()에 sandbox/cwd/approval_mode를 다시 넘기지 않으면 원 thread의
        enforcement 핀(예: REVIEWER의 read_only sandbox, deny_all approval)이 조용히
        thread_resume() 자체 기본값으로 완화될 수 있다 — 여기서는 start_session이
        캐시해 둔 설정을 재전달해 그 핀을 유지한다.

        effort는 thread_resume() 파라미터가 아니다 (installed openai_codex==0.144.4
        AsyncCodex.thread_resume은 approval_mode/base_instructions/config/cwd/
        developer_instructions/model/model_provider/personality/sandbox/service_tier만
        받는다 — api.py:443-453). effort와 output_schema는 모두 turn 단위 인자
        (thread.run(effort=..., output_schema=...))이므로 여기서는 self._efforts와
        self._schemas를 복원해 이어지는 send()가 둘 다 None을 넘기지 않게 한다.
        base_instructions(system_prompt)는 캐시하지 않으므로 resume 이후에는
        재적용되지 않는다 — claude_code.py의 동일 갭(B1)과 같은 성격.

        한계: 캐시는 이 adapter 인스턴스의 메모리에만 있다. start_session을 거치지
        않은 session_id로 resume()이 불리면(예: 프로세스 재시작 후 recovery — Claude
        adapter의 동일 갭이 deferred B1로 등재돼 있다) 캐시가 비어 있어 thread_resume()
        기본값을 그대로 쓰게 된다. Cross-process 복구는 registry에 provider_ref로
        thread_id를 색인하는 후속 작업이 필요하며 이번 fix wave 범위 밖이다.
        """
        codex = await self._client()
        config = self._session_config.get(session_id)
        if config is not None:
            resumed = await codex.thread_resume(
                session_id,
                sandbox=Sandbox[config["sandbox_name"]],
                cwd=config["cwd"],
                approval_mode=ApprovalMode[config["approval_mode_name"]],
            )
            self._efforts[session_id] = config["effort"]
            self._schemas[session_id] = config.get("output_schema")
        else:
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
