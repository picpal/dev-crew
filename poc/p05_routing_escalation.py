"""POC 5·6 — live: CHEAP spawn → NEED_REPLAN 가정 → HIGH_CAPABILITY 새 instance가
handoff를 받아 이어받는지. LLM 호출은 2회로 제한."""
import asyncio

from _common import record, stores


async def main():
    from devcrew.adapters.claude_code import ClaudeCodeAdapter
    from devcrew.orchestrator import Orchestrator
    from devcrew.schema import Provider, Role

    trace, registry = stores()
    claude = ClaudeCodeAdapter(trace, registry)
    orch = Orchestrator(trace, registry, {Provider.CLAUDE_CODE: claude})

    dev1 = await orch.spawn(Role.DEVELOPER, "CHEAP", execution_id="POC-5",
                            node_id="n1", task_scope="정렬 함수")
    sid = await claude.start_session(dev1, "한 줄로 답해: 파이썬 리스트 정렬 함수 이름은?")
    registry.checkpoint(dev1.instance_id, summary="sorted() 확인, 구현 미완",
                        artifacts=[])
    await claude.archive(sid)

    dev2 = await orch.spawn(Role.DEVELOPER, "HIGH_CAPABILITY", execution_id="POC-5",
                            node_id="n1", task_scope="정렬 함수",
                            replaced=dev1, escalation_reason="NEED_REPLAN")
    esc = trace.events(event_type="ModelEscalationEvent")[0]["payload"]
    sid2 = await claude.start_session(
        dev2, f"이전 시도 요약: {esc['handoff']['last_summary']}. 이어서 한 줄로 답해: "
              "안정 정렬 보장 여부는?")
    await claude.archive(sid2)

    record("p05", {
        "escalation_event_recorded": esc["reason"] == "NEED_REPLAN",
        "new_instance_higher_tier": dev2.model == "claude-opus-5",
        "handoff_summary_delivered": "sorted" in esc["handoff"]["last_summary"],
        "old_instance_out_of_registry": all(
            r["instance_id"] != dev1.instance_id for r in registry.active()),
    })


asyncio.run(main())
