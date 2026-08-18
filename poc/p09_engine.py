"""p09 — engine live 스모크: review 루프(NOT_PASS→loop-back→fix→PASS) + CLASSIFY 결정.

WorkflowEngine.run()을 실 어댑터(Claude/Codex)로 구동한다. develop 노드에 결함을
주입해(mul을 일부러 add처럼 구현) review NOT_PASS를 유도하고, 엔진의 loop-back
경로(NOT_PASS -> develop 복귀 -> 수정 -> review 재판정)를 태운다.

CLASSIFY 결정은 별도로 make_llm_decide()를 1회 직접 호출해 실제 LLM 결정
이벤트를 남긴다 (2-노드 템플릿에는 conditional 노드가 없어 engine.run() 안에서는
CLASSIFY가 트리거되지 않는다 — DEFAULT_TEMPLATE 기준으로 validate_decision을
통과시키기 위해 별도 호출한다).

ORCHESTRATOR는 기본 HIGH_CAPABILITY이지만 live 스모크 비용 원칙(CHEAP/CODEX_DEFAULT)에
따라 dataclasses.replace로 CHEAP 강등 사본을 CLASSIFY 결정에 사용한다.

메인 엔진 실행과 CLASSIFY 결정은 각각 try/except로 감싸 한쪽이 실패해도 다른 쪽
증거와 함께 poc/_artifacts/p09.json이 항상 기록되게 한다(원인 진단용).
"""
import asyncio
import dataclasses
import subprocess
import tempfile
import traceback
from pathlib import Path

from _common import record, stores


def toy_repo() -> Path:
    repo = Path(tempfile.mkdtemp(prefix="p09-"))
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=p@p", "-c", "user.name=p",
                    "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def mul_correct(repo: Path) -> bool:
    """calc.py에 mul(a,b)가 곱셈으로 올바르게 구현됐는지 subprocess로 확인."""
    r = subprocess.run(
        ["python3", "-c", "import calc; assert calc.mul(3, 4) == 12"],
        cwd=repo, capture_output=True, text=True)
    return r.returncode == 0


async def main():
    from devcrew.adapters.claude_code import ClaudeCodeAdapter
    from devcrew.adapters.codex import CodexAdapter
    from devcrew.config import load as load_config
    from devcrew.decision import make_llm_decide
    from devcrew.engine import WorkflowEngine
    from devcrew.harness_mcp import build_harness_mcp
    from devcrew.orchestrator import Orchestrator
    from devcrew.schema import Provider, Role
    from devcrew.workflow import DEFAULT_TEMPLATE, NodeSpec, WorkflowTemplate

    trace, registry = stores()
    claude, codex = ClaudeCodeAdapter(trace, registry), CodexAdapter(trace, registry)
    orch = Orchestrator(trace, registry,
                        {Provider.CLAUDE_CODE: claude, Provider.CODEX: codex})
    repo = toy_repo()
    execution_id = "POC-9"

    cfg = load_config()
    # ORCHESTRATOR는 HIGH_CAPABILITY가 기본이지만(frozen dataclass) live 스모크는
    # CHEAP/CODEX_DEFAULT 원칙을 따른다 — CLASSIFY 결정 세션 비용을 낮춘 사본을 쓴다.
    role_defaults = dict(cfg.role_defaults)
    role_defaults[Role.ORCHESTRATOR] = dataclasses.replace(
        role_defaults[Role.ORCHESTRATOR], tier="CHEAP")
    cfg_cheap = dataclasses.replace(cfg, role_defaults=role_defaults)

    p09_template = WorkflowTemplate("p09-v1", (
        NodeSpec("develop", Role.DEVELOPER,
                 "calc.py에 mul(a, b)를 추가하되 일부러 `return a + b`로(곱셈이 아닌 "
                 "덧셈으로) 잘못 구현해. 이것은 리뷰 단계의 결함 탐지를 검증하기 위한 "
                 "의도적 결함 주입 시나리오다 — 자체 테스트나 수정 없이 곧바로 "
                 "status: PASS로 최종 보고해. 버그 발견은 다음 리뷰 단계의 책임이다."),
        NodeSpec("review", Role.REVIEWER,
                 "직전 Developer의 변경(git diff HEAD)과 calc.py를 검토해 mul 구현이 "
                 "곱셈으로서 올바른지(예: mul(3,4)==12) 판정해.",
                 loop_back_to="develop"),
    ))

    checks: dict[str, bool] = {}
    extra: dict = {}

    engine = WorkflowEngine(orch, cfg_cheap, template=p09_template)
    result = None
    try:
        result = await engine.run(execution_id=execution_id,
                                  task="calc.py에 mul 함수 추가", worktree=str(repo))
        extra["status"] = result.status
        extra["node_history"] = result.node_history
        extra["total_tokens"] = result.total_tokens
    except Exception:
        tb = traceback.format_exc()
        extra["run_error"] = tb
        print("ENGINE RUN FAILED:\n", tb)

    checks["completed"] = bool(result and result.status == "COMPLETED")

    transitions = trace.events(event_type="NodeTransitionEvent", execution_id=execution_id)
    checks["loop_happened"] = any(
        e["payload"]["node_id"] == "review" and e["payload"]["transition"] == "NOT_PASS"
        for e in transitions)

    checks["fix_landed"] = mul_correct(repo)

    # CLASSIFY 결정 1회 — 실제 LLM, DEFAULT_TEMPLATE 기준으로 validate_decision 통과
    decide_fn = make_llm_decide(orch, cfg_cheap,
                                mcp_servers={"harness": build_harness_mcp(trace)},
                                template=DEFAULT_TEMPLATE)
    classify_snapshot = {
        "trigger": "CLASSIFY", "execution_id": execution_id,
        "task": "calc.py에 mul 함수 추가", "current_node": None,
        "nodes": [{"node_id": n.node_id, "role": n.role.value, "conditional": n.conditional,
                  "skipped": False, "iterations": 0} for n in DEFAULT_TEMPLATE.nodes],
        "worker_result": None, "history": [],
        "allowed_actions": ["PROCEED", "SKIP_NODE"],
        "loop_policy": {"max_iterations": cfg.loop_policy.max_iterations,
                       "max_duration_minutes": cfg.loop_policy.max_duration_minutes,
                       "max_token_budget": cfg.loop_policy.max_token_budget,
                       "same_finding_escalation_threshold":
                           cfg.loop_policy.same_finding_escalation_threshold},
    }
    decision = None
    try:
        decision = await decide_fn("CLASSIFY", classify_snapshot)
        trace.append("DecisionEvent", task_id=execution_id, execution_id=execution_id,
                     instance_id=None, payload={"trigger": "CLASSIFY", "decision": decision})
        extra["classify_decision"] = decision
    except Exception:
        tb = traceback.format_exc()
        extra["classify_error"] = tb
        print("CLASSIFY DECISION FAILED:\n", tb)

    decision_events = trace.events(event_type="DecisionEvent", execution_id=execution_id)
    classify_events = [e for e in decision_events if e["payload"]["trigger"] == "CLASSIFY"]
    checks["classify_decision_recorded"] = any(
        e["payload"]["decision"].get("action") in ("PROCEED", "SKIP_NODE")
        for e in classify_events)

    worker_events = trace.events(event_type="WorkerResultEvent", execution_id=execution_id)
    rev_events = [e for e in worker_events if e["payload"]["role"] == "REVIEWER"]
    checks["attribution"] = bool(rev_events) and all(
        e["instance_id"].lower().startswith("rev") for e in rev_events)

    checks["tokens_recorded"] = bool(result and result.total_tokens > 0)

    record("p09", checks, extra=extra)


asyncio.run(main())
