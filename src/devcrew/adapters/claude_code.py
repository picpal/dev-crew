"""Claude Code Adapter (#11) — ClaudeSDKClient(streaming), archive는 합성."""
from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from ..enforcement import claude_options_kwargs, make_can_use_tool
from ..schema import AgentInstance, Usage
from ..store.registry import SessionRegistry
from ..store.trace import TraceStore
from .base import ResumeConfigMissingError, TurnOutcome


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
        # session_id -> start_session()이 사용한 옵션. resume()이 이를 재주입해
        # role enforcement/구조화 출력 강제를 유지한다 (finding #1).
        self._start_opts: dict[str, dict] = {}
        # session_id -> start_session()의 최초 turn usage. 그 turn은 session_id 자체를
        # 반환값으로 만들어내는 turn이라 outcome 전체가 호출자에게 버려지는데, 그
        # 소비 토큰은 실제로 발생한 비용이다 — engine이 첫 방문 직후 합산할 수 있게
        # 별도로 캐시해 initial_usage()로 노출한다 (finding #6).
        self._initial_usage: dict[str, Usage] = {}

    async def start_session(self, inst: AgentInstance, initial_message: str, *,
                             system_prompt: str | None = None,
                             output_schema: dict | None = None,
                             mcp_servers: dict | None = None) -> str:
        kw = claude_options_kwargs(inst.role, cwd=inst.worktree)
        options = ClaudeAgentOptions(
            model=inst.model,
            effort=inst.effort_level.value.lower(),
            can_use_tool=make_can_use_tool(inst.role, self.trace, task_id=inst.execution_id,
                                           workspace_root=inst.worktree),
            system_prompt=system_prompt,
            output_format={"type": "json_schema", "schema": output_schema} if output_schema else None,
            mcp_servers=mcp_servers or {},
            **kw,
        )
        client = ClaudeSDKClient(options)
        await client.connect()
        try:
            outcome = await self._turn(client, initial_message)
        except BaseException:
            # 첫 turn이 끝나야 session_id가 나온다. 여기서 실패하거나 상한에 걸려
            # 취소되면 `_clients`에 등록되지 않아 `archive()`로 회수할 방법이 없고,
            # 워커 프로세스는 고아로 남아 계속 돈다 (2026-08-24). CancelledError는
            # Exception이 아니므로 BaseException으로 받는다.
            try:
                await client.disconnect()
            except Exception:
                pass
            raise
        session_id = outcome.raw["session_id"]
        self._clients[session_id] = client
        self._initial_usage[session_id] = outcome.usage
        self._start_opts[session_id] = {
            "system_prompt": system_prompt,
            "output_schema": output_schema,
            "mcp_servers": mcp_servers,
            "role": inst.role,
            "worktree": inst.worktree,
            "model": inst.model,
            "effort": inst.effort_level.value.lower(),
            "execution_id": inst.execution_id,
        }
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
            structured=getattr(result_msg, "structured_output", None),
        )

    async def send(self, session_id: str, message: str) -> TurnOutcome:
        return await self._turn(self._clients[session_id], message)

    async def resume(self, session_id: str, message: str, *,
                      allow_unconfigured: bool = False) -> TurnOutcome:
        """세션 재개 — start_session에서 캐시해 둔 시작 설정을 재주입한다 (finding #1).

        같은 adapter 인스턴스에서 이 session_id로 start_session이 먼저 호출됐다면
        system_prompt/output_format/mcp_servers/allowed_tools/permission_mode/
        can_use_tool/cwd/model/effort를 모두 복원해 role 경계와 구조화 출력 강제,
        harness MCP tool 노출이 resume 이후에도 유지된다 (Task 6).

        캐시가 없으면 (예: 프로세스 재시작으로 새 adapter 인스턴스가 만들어진 경우)
        기본적으로 ResumeConfigMissingError로 fail-closed 한다 — enforcement 없이
        조용히 resume을 허용하면 이전의 fail-open 취약점이 재발한다. Cross-process
        recovery처럼 의도적으로 설정 없는 resume이 필요한 호출자는
        allow_unconfigured=True를 넘겨야 한다 (poc/p06_recovery.py phase_b가 이
        경로를 쓴다 — registry 기반 cross-process 설정 복원은 deferred B1, 범위 밖).
        """
        opts = self._start_opts.get(session_id)
        if opts is None:
            if not allow_unconfigured:
                raise ResumeConfigMissingError(
                    f"no cached start config for session {session_id} in this adapter "
                    "instance; pass allow_unconfigured=True to resume without role "
                    "enforcement/output schema (deferred B1)"
                )
            options = ClaudeAgentOptions(resume=session_id)
        else:
            kw = claude_options_kwargs(opts["role"], cwd=opts["worktree"])
            options = ClaudeAgentOptions(
                resume=session_id,
                model=opts["model"],
                effort=opts["effort"],
                can_use_tool=make_can_use_tool(opts["role"], self.trace,
                                               task_id=opts["execution_id"],
                                               workspace_root=opts["worktree"]),
                system_prompt=opts["system_prompt"],
                output_format={"type": "json_schema", "schema": opts["output_schema"]}
                if opts["output_schema"] else None,
                mcp_servers=opts.get("mcp_servers") or {},
                **kw,
            )
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

    async def context_usage(self, session_id: str) -> dict | None:
        """세션의 **실제** 컨텍스트 창 점유 — CLI `/context`와 같은 데이터.

        토큰 합산으로 창 점유를 추정할 필요가 없다: SDK가 실효 한도(autocompact
        버퍼 반영)와 퍼센트를 직접 준다. 세션이 이 프로세스 밖이면 None.
        """
        client = self._clients.get(session_id)
        if client is None:
            return None
        try:
            raw = await client.get_context_usage()
        except Exception:
            return None                # 조회 실패가 답변을 막지 않는다 — 추정으로 폴백
        window = raw.get("maxTokens") or raw.get("rawMaxTokens") or 0
        return {"used": int(raw.get("totalTokens") or 0), "window": int(window),
                "pct": float(raw.get("percentage") or 0.0),
                "model": raw.get("model") or "",
                "autocompact": raw.get("autoCompactThreshold"),
                "source": "sdk"}

    async def initial_usage(self, session_id: str) -> Usage | None:
        """start_session이 소비한 최초 turn의 usage (finding #6). 캐시가 없으면 None
        (예: 이 session_id가 이 adapter 인스턴스의 start_session을 거치지 않음)."""
        return self._initial_usage.get(session_id)
