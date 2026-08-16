"""POC 10 — 재시작 후 lazy verify → resume 성공 + FAILED_RECOVERY 경로 (§6.3, #15).

사용법:
  uv run python poc/p06_recovery.py phase_a   # 세션 만들고 프로세스 종료
  uv run python poc/p06_recovery.py phase_b   # '재시작': 복구 검증
"""
import asyncio
import sys

from _common import POC_DIR, record, stores


async def phase_a():
    from devcrew.adapters.claude_code import ClaudeCodeAdapter, transcript_path
    from devcrew.orchestrator import Orchestrator
    from devcrew.schema import Provider, Role

    trace, registry = stores()
    claude = ClaudeCodeAdapter(trace, registry)
    orch = Orchestrator(trace, registry, {Provider.CLAUDE_CODE: claude})

    inst = await orch.spawn(Role.DEVELOPER, "CHEAP", execution_id="POC-6",
                            node_id="n1", task_scope="*")
    sid = await claude.start_session(inst, "암호 '735'를 기억해. '기억했다'라고만 답해.")
    inst.session_id = sid
    registry.upsert(inst, provider_ref=str(transcript_path(sid, ".")))
    registry.checkpoint(inst.instance_id, summary="암호 기억 세션", artifacts=[])

    # 죽은 세션 시뮬레이션: 존재하지 않는 transcript를 가리키는 유령 행
    ghost = await orch.spawn(Role.DEVELOPER, "CHEAP", execution_id="POC-6",
                             node_id="n2", task_scope="*")
    registry.upsert(ghost, provider_ref="/nonexistent/transcript.jsonl")
    print("phase_a done — 프로세스를 종료합니다. phase_b를 실행하세요.")


async def phase_b():
    from pathlib import Path

    from devcrew.adapters.claude_code import ClaudeCodeAdapter
    from devcrew.schema import InstanceStatus

    trace, registry = stores()          # 새 프로세스 = 재시작
    verdicts = registry.lazy_verify({
        "CLAUDE_CODE": lambda row: bool(row["provider_ref"])
                                    and Path(row["provider_ref"]).exists(),
    })
    live = [k for k, v in verdicts.items() if v == "RESUMABLE"]
    dead = [k for k, v in verdicts.items() if v == "FAILED_RECOVERY"]

    # RESUMABLE 행을 실제 resume — 컨텍스트 유지 확인 (여기서 처음 LLM 호출)
    claude = ClaudeCodeAdapter(trace, registry)
    row = next(r for r in registry.active() if r["instance_id"] == live[0])
    out = await claude.resume(row["body"]["session_id"], "기억한 암호는? 숫자만.")

    # FAILED_RECOVERY 행: handoff 만들고 registry에서 제거
    h = registry.handoff(dead[0])
    h["reason"] = "FAILED_RECOVERY"
    trace.append("RecoveryEvent", task_id="POC-6", instance_id=dead[0],
                 payload=h)
    registry.finish(dead[0])

    record("p06", {
        "lazy_verify_no_llm_call": True,      # verify 단계는 파일 검사만 했다
        "one_resumable_one_failed": len(live) == 1 and len(dead) == 1,
        "resume_keeps_context": "735" in out.text,
        "failed_recovery_handoff_recorded":
            trace.events(event_type="RecoveryEvent")[0]["payload"]["reason"]
            == "FAILED_RECOVERY",
    })


asyncio.run(phase_a() if sys.argv[1:] == ["phase_a"] else phase_b())
