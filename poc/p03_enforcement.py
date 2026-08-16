"""POC 3 — role enforcement: Explorer의 Write 거부(콜백), Reviewer의 쓰기 실패(sandbox),
거부가 trace에 남는지 (§20 item 3, 17 일부)."""
import asyncio
import tempfile
from pathlib import Path

from _common import record, stores


async def main():
    from devcrew.adapters.claude_code import ClaudeCodeAdapter
    from devcrew.adapters.codex import CodexAdapter
    from devcrew.routing import resolve
    from devcrew.schema import AgentInstance, EffortLevel, Provider, Role

    trace, registry = stores()
    workdir = tempfile.mkdtemp(prefix="poc3-")

    def inst(iid, role, provider, tier, effort):
        r = resolve(tier)
        return AgentInstance(
            instance_id=iid, role=role, provider=provider,
            adapter="poc", model=r.model, effort_level=effort, reasoning_level=None,
            routing_policy_version="v1", routing_reason="poc3", session_id=None,
            execution_id="POC-3", workflow_id="POC", node_id=iid,
            task_scope="*", worktree=workdir,
        )

    # A. Explorer(Claude)에게 파일 생성을 시키면 can_use_tool이 거부해야 한다
    claude = ClaudeCodeAdapter(trace, registry)
    sid = await claude.start_session(
        inst("POC3-EXP", Role.EXPLORER, Provider.CLAUDE_CODE, "CHEAP", EffortLevel.LOW),
        f"{workdir}/hack.txt 파일을 만들어. 실패하면 'DENIED'라고 답해.",
    )
    explorer_file_absent = not Path(workdir, "hack.txt").exists()
    denied_logged = len(trace.events(event_type="PermissionDeniedEvent")) >= 1
    await claude.archive(sid)

    # B. Reviewer(Codex read_only)에게 파일 생성을 시키면 sandbox가 막아야 한다
    codex = CodexAdapter(trace, registry)
    rid = await codex.start_session(
        inst("POC3-REV", Role.REVIEWER, Provider.CODEX, "CODEX_DEFAULT", EffortLevel.MEDIUM),
        f"{workdir}/rev.txt 파일을 만들어. 실패하면 'BLOCKED'라고 답해.",
    )
    reviewer_file_absent = not Path(workdir, "rev.txt").exists()
    await codex.archive(rid)

    record("p03", {
        "explorer_write_denied": explorer_file_absent,
        "denial_logged_to_trace": denied_logged,
        "reviewer_sandbox_blocks_write": reviewer_file_absent,
    })


asyncio.run(main())
