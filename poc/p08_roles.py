"""p08 — role 번들 live 스모크: 4 worker role이 스키마 적합 출력을 내는가.

각 role CHEAP/CODEX_DEFAULT 1회, 장난감 repo 대상. 총 4 LLM 호출.
"""
import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

from _common import record, stores

STATUS_ENUM = {"PASS", "NOT_PASS", "NEED_REPLAN", "BLOCKED", "INSUFFICIENT_CAPABILITY"}


def toy_repo() -> Path:
    repo = Path(tempfile.mkdtemp(prefix="p08-"))
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=p@p", "-c", "user.name=p",
                    "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


async def main():
    from devcrew.adapters.claude_code import ClaudeCodeAdapter
    from devcrew.adapters.codex import CodexAdapter
    from devcrew.orchestrator import Orchestrator
    from devcrew.schema import Provider, Role

    trace, registry = stores()
    claude, codex = ClaudeCodeAdapter(trace, registry), CodexAdapter(trace, registry)
    orch = Orchestrator(trace, registry,
                        {Provider.CLAUDE_CODE: claude, Provider.CODEX: codex})
    repo = toy_repo()
    checks: dict[str, bool] = {}
    outputs: dict[str, dict] = {}

    async def run_role(name, role, tier, prompt):
        inst = await orch.spawn(role, tier, execution_id="POC-8",
                                node_id=name, task_scope="*", worktree=str(repo))
        sid = await orch.start_worker(inst, prompt)
        adapter = orch.adapters[inst.provider]
        out = await adapter.send(sid, "이제 최종 보고를 스키마대로 제출해.")
        # 소비 경계(orchestrator.consume_result)를 실제로 통과시킨다 — structured
        # 출력을 직접 들여다보는 대신 이 경로로 상태 전이값을 얻어야 role 검증/
        # Reviewer verdict 규칙이 실제 사용된다(재리뷰 finding #3).
        transition = orch.consume_result(inst, out)
        s = out.structured
        outputs[name] = s or {}
        checks[f"{name}_structured"] = isinstance(s, dict)
        checks[f"{name}_status_valid"] = isinstance(s, dict) and s.get("status") in STATUS_ENUM
        checks[f"{name}_transition_valid"] = transition in STATUS_ENUM | {"PASS", "NOT_PASS"}
        await adapter.archive(sid)
        registry.finish(inst.instance_id)

    await run_role("explorer", Role.EXPLORER, "CHEAP",
                   "calc.py에서 sub 함수의 위치와 add와의 차이를 조사해 보고해.")
    await run_role("developer", Role.DEVELOPER, "CHEAP",
                   "calc.py에 mul(a, b) 함수를 추가하고 python -c로 동작 확인 후 보고해.")
    await run_role("reviewer", Role.REVIEWER, "CODEX_DEFAULT",
                   "git diff HEAD와 calc.py를 읽고 mul 추가가 적절한지 검토 판정해.")
    await run_role("qa", Role.QA, "CHEAP",
                   "acceptance criteria: 'mul(3,4)==12'. 검증 계획을 세우고 실행해 보고해.")

    checks["explorer_has_findings"] = bool(outputs["explorer"].get("findings"))
    checks["developer_changed_files"] = "calc.py" in " ".join(
        outputs["developer"].get("changed_files", []))
    checks["reviewer_verdict_present"] = outputs["reviewer"].get("verdict") in {"PASS", "NOT_PASS"}
    checks["qa_results_present"] = bool(outputs["qa"].get("results"))

    record("p08", checks, extra={"outputs": outputs})


asyncio.run(main())
