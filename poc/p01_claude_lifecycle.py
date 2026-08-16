"""POC 1 — Claude Code: 새 session, follow-up, resume, cancel, usage 수집 (§20)."""
import asyncio

from _common import record, stores


async def main():
    from devcrew.adapters.claude_code import ClaudeCodeAdapter
    from devcrew.routing import resolve
    from devcrew.schema import AgentInstance, EffortLevel, Provider, Role

    trace, registry = stores()
    r = resolve("CHEAP")
    inst = AgentInstance(
        instance_id="POC1-EXPLORER", role=Role.EXPLORER,
        provider=Provider.CLAUDE_CODE, adapter="claude-code-adapter",
        model=r.model, effort_level=EffortLevel.LOW, reasoning_level=None,
        routing_policy_version="v1", routing_reason="poc1 cheap tier",
        session_id=None, execution_id="POC-1", workflow_id="POC",
        node_id="p01", task_scope="README only", worktree=None,
    )
    adapter = ClaudeCodeAdapter(trace, registry)

    sid = await adapter.start_session(inst, "핀 번호 1717을 기억해. '기억했다'라고만 답해.")
    follow = await adapter.send(sid, "기억한 핀 번호는? 숫자만 답해.")
    memory_kept = "1717" in follow.text
    usage_ok = (follow.usage.output_tokens or 0) > 0
    cost_present = follow.usage.total_cost_usd is not None

    await adapter.archive(sid)                       # 연결 종료
    resumed = await adapter.resume(sid, "핀 번호를 다시 말해. 숫자만.")
    resume_kept = "1717" in resumed.text

    record("p01", {
        "start_session_returns_id": bool(sid),
        "follow_up_keeps_context": memory_kept,
        "usage_collected": usage_ok,
        "cost_estimate_present": cost_present,
        "resume_after_disconnect_keeps_context": resume_kept,
    }, extra={"session_id": sid, "raw_usage": follow.usage.raw})


asyncio.run(main())
