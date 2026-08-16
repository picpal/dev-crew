"""Claude Code Adapter (#11) — ClaudeSDKClient(streaming), archive는 합성."""
from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from ..enforcement import claude_options_kwargs, make_can_use_tool
from ..schema import AgentInstance, Usage
from ..store.registry import SessionRegistry
from ..store.trace import TraceStore
from .base import TurnOutcome


def _usage_from_result(result_msg) -> Usage:
    u = getattr(result_msg, "usage", None) or {}
    return Usage(
        input_tokens=u.get("input_tokens"),
        output_tokens=u.get("output_tokens"),
        cache_creation_input_tokens=u.get("cache_creation_input_tokens"),
        cache_read_input_tokens=u.get("cache_read_input_tokens"),
        total_cost_usd=getattr(result_msg, "total_cost_usd", None),
        raw={"usage": dict(u),
             "model_usage": getattr(result_msg, "model_usage", None) or {}},
    )


def transcript_path(session_id: str, cwd: str) -> Path:
    """~/.claude/projects/<encoded-cwd>/<session-id>.jsonl (session-surfaces.md §3.5)"""
    encoded = "".join(c if c.isalnum() else "-" for c in str(Path(cwd).resolve()))
    return Path.home() / ".claude" / "projects" / encoded / f"{session_id}.jsonl"


class ClaudeCodeAdapter:
    def __init__(self, trace: TraceStore, registry: SessionRegistry):
        self.trace = trace
        self.registry = registry
        self._clients: dict[str, ClaudeSDKClient] = {}

    async def start_session(self, inst: AgentInstance, initial_message: str) -> str:
        kw = claude_options_kwargs(inst.role, cwd=inst.worktree)
        options = ClaudeAgentOptions(
            model=inst.model,
            effort=inst.effort_level.value.lower(),
            can_use_tool=make_can_use_tool(inst.role, self.trace, task_id=inst.execution_id),
            **kw,
        )
        client = ClaudeSDKClient(options)
        await client.connect()
        outcome = await self._turn(client, initial_message)
        session_id = outcome.raw["session_id"]
        self._clients[session_id] = client
        return session_id

    async def _turn(self, client: ClaudeSDKClient, message: str) -> TurnOutcome:
        await client.query(message)
        text, result_msg = [], None
        async for msg in client.receive_response():
            kind = type(msg).__name__
            if kind == "AssistantMessage":
                for block in getattr(msg, "content", []):
                    if hasattr(block, "text"):
                        text.append(block.text)
            elif kind == "ResultMessage":
                result_msg = msg
        usage = _usage_from_result(result_msg)
        return TurnOutcome(
            text="".join(text),
            usage=usage,
            raw={"session_id": getattr(result_msg, "session_id", None), **usage.raw},
        )

    async def send(self, session_id: str, message: str) -> TurnOutcome:
        return await self._turn(self._clients[session_id], message)

    async def resume(self, session_id: str, message: str) -> TurnOutcome:
        """프로세스 재시작 후 경로 — 새 client를 resume 옵션으로 연결."""
        options = ClaudeAgentOptions(resume=session_id)
        client = ClaudeSDKClient(options)
        await client.connect()
        self._clients[session_id] = client
        return await self._turn(client, message)

    async def cancel(self, session_id: str) -> str:
        await self._clients[session_id].interrupt()
        return "CANCELLED"

    async def archive(self, session_id: str) -> str:
        """합성 archive (#11): harness 상태 + transcript 보관. 파싱 금지."""
        client = self._clients.pop(session_id, None)
        if client:
            await client.disconnect()
        return "ARCHIVED"

    async def get_usage(self, session_id: str) -> Usage:
        raise NotImplementedError("usage는 각 TurnOutcome.usage로 수집한다")
