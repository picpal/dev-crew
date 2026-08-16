"""POC 2 — Codex Reviewer: 지정 model/effort로 생성·follow-up·resume·archive·usage (§20)."""
import asyncio

from _common import record, stores


async def main():
    from devcrew.adapters.codex import CodexAdapter
    from devcrew.routing import resolve
    from devcrew.schema import AgentInstance, EffortLevel, Provider, Role

    trace, registry = stores()
    r = resolve("CODEX_DEFAULT")
    inst = AgentInstance(
        instance_id="POC2-REV", role=Role.REVIEWER, provider=Provider.CODEX,
        adapter="codex-adapter", model=r.model, effort_level=EffortLevel.MEDIUM,
        reasoning_level=None, routing_policy_version="v1",
        routing_reason="poc2 codex default", session_id=None,
        execution_id="POC-2", workflow_id="POC", node_id="p02",
        task_scope="review", worktree=None,
    )
    adapter = CodexAdapter(trace, registry)

    sid = await adapter.start_session(inst, "코드명 '청록'을 기억해. '기억했다'라고만 답해.")
    follow = await adapter.send(sid, "기억한 코드명은? 단어만 답해.")
    resumed = await adapter.resume(sid, "코드명을 다시 말해. 단어만.")
    archived = await adapter.archive(sid)

    record("p02", {
        "thread_started": bool(sid),
        "follow_up_keeps_context": "청록" in follow.text,
        "usage_tokens_collected": (follow.usage.output_tokens or 0) > 0,
        "duration_ms_native": follow.usage.duration_ms is not None,
        "reasoning_tokens_separate": follow.usage.reasoning_output_tokens is not None,
        "resume_keeps_context": "청록" in resumed.text,
        "archive_native": archived == "ARCHIVED",
    }, extra={"thread_id": sid, "raw": follow.usage.raw})


asyncio.run(main())
