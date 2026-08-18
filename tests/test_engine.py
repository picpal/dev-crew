import dataclasses

import pytest
from devcrew.adapters.base import FakeAdapter
from devcrew.config import LoopPolicy, load as load_config
from devcrew.engine import ExecutionResult, WorkflowEngine
from devcrew.orchestrator import Orchestrator
from devcrew.schema import Provider, Role
from devcrew.store.registry import SessionRegistry
from devcrew.store.trace import TraceStore
from devcrew.workflow import DEFAULT_TEMPLATE, NodeSpec, WorkflowError, WorkflowTemplate

pytestmark = pytest.mark.asyncio

# 시뮬용 2-노드 템플릿 (explore/qa 없이 develop->review 루프만)
SIM = WorkflowTemplate("sim", (
    NodeSpec("develop", Role.DEVELOPER, "구현: {task}"),
    NodeSpec("review", Role.REVIEWER, "검토: {task}", loop_back_to="develop"),
))

PASS_DEV = {"status": "PASS", "summary": "done", "changed_files": ["a.py"],
            "build": {"ok": True, "detail": ""}, "tests": {"passed": 1, "failed": 0, "detail": ""}}
GENERIC_PASS = {"status": "PASS", "summary": "ok"}
PASS_REVIEW = {"status": "PASS", "summary": "ok", "verdict": "PASS", "findings": []}
FAIL_REVIEW = {"status": "PASS", "summary": "issues", "verdict": "NOT_PASS",
               "findings": [{"severity": "major", "file": "a.py", "line": 1, "description": "bug"}]}
BLOCKED_DEV = {"status": "BLOCKED", "summary": "stuck", "changed_files": [],
              "build": {"ok": False, "detail": ""}, "tests": {"passed": 0, "failed": 0, "detail": ""}}
NEED_REPLAN_DEV = {"status": "NEED_REPLAN", "summary": "?", "changed_files": [],
                   "build": {"ok": False, "detail": ""},
                   "tests": {"passed": 0, "failed": 0, "detail": ""}}


def make_engine(tmp_path, dev_script=None, review_script=None, decide_fn=None,
                template=SIM, clock=None, cfg=None):
    """FakeAdapter는 send() 호출을 세션별(session_id별)로 개별 카운트해 소비한다
    (start_session이 새 세션마다 0부터 카운트를 리셋) — 한 provider를 여러 노드가
    공유해도 노드마다 독립된 세션이므로, provider(CLAUDE_CODE/CODEX)별로 어댑터를
    분리해 각 role의 방문 순서대로 스크립트를 소비하게 한다.
    """
    trace = TraceStore(tmp_path / "trace.db")
    registry = SessionRegistry(tmp_path / "harness.db")
    dev_fake = FakeAdapter(structured_script=dev_script or [])
    review_fake = FakeAdapter(structured_script=review_script or [])
    orch = Orchestrator(trace, registry,
                        {Provider.CLAUDE_CODE: dev_fake, Provider.CODEX: review_fake})
    kwargs = {"template": template, "decide_fn": decide_fn}
    if clock:
        kwargs["clock"] = clock
    return WorkflowEngine(orch, cfg or load_config(), **kwargs), trace, (dev_fake, review_fake)


async def test_straight_pass(tmp_path):
    engine, trace, _ = make_engine(tmp_path, [PASS_DEV], [PASS_REVIEW])
    r = await engine.run(execution_id="E1", task="t")
    assert r.status == "COMPLETED"
    assert [h["node_id"] for h in r.node_history] == ["develop", "review"]


async def test_review_loop_then_pass(tmp_path):
    # dev PASS -> review NOT_PASS -> dev fix PASS -> review PASS
    engine, trace, _ = make_engine(tmp_path, [PASS_DEV, PASS_DEV], [FAIL_REVIEW, PASS_REVIEW])
    r = await engine.run(execution_id="E2", task="t")
    assert r.status == "COMPLETED"
    assert r.node_history[1]["transition"] == "NOT_PASS"


async def test_attribution_reviewer_result_recorded_under_reviewer_instance(tmp_path):
    engine, trace, _ = make_engine(tmp_path, [PASS_DEV, PASS_DEV], [FAIL_REVIEW, PASS_REVIEW])
    await engine.run(execution_id="E3", task="t")
    events = trace.events(event_type="WorkerResultEvent")
    rev_events = [e for e in events if e["payload"]["role"] == "REVIEWER"]
    dev_events = [e for e in events if e["payload"]["role"] == "DEVELOPER"]
    assert rev_events and dev_events
    # Reviewer 이벤트의 instance_id는 REV- prefix (spawn의 role 앞 3글자 규칙)
    assert all(e["instance_id"].startswith("REV") for e in rev_events)
    assert all(e["instance_id"].startswith("DEV") for e in dev_events)
    # 회귀 고정: reviewer 이벤트가 developer instance로 귀속되지 않는다 (#18 park 해소)
    assert not any(e["instance_id"].startswith("DEV") for e in rev_events)


async def test_worker_result_event_payload_includes_structured(tmp_path):
    engine, trace, _ = make_engine(tmp_path, [PASS_DEV], [PASS_REVIEW])
    await engine.run(execution_id="E3b", task="t")
    events = trace.events(event_type="WorkerResultEvent")
    assert events and events[0]["payload"]["structured"] == PASS_DEV


async def test_same_finding_guard_triggers_decision(tmp_path):
    # 동일 summary의 NOT_PASS 3연속 -> LOOP_GUARD_EXCEEDED -> decide_fn 호출 -> ABORT
    calls = []

    async def decide(trigger, snapshot):
        calls.append(trigger)
        return {"action": "ABORT", "target_node": None, "rationale": "test"}

    engine, trace, _ = make_engine(tmp_path, [PASS_DEV] * 3, [FAIL_REVIEW] * 3, decide_fn=decide)
    r = await engine.run(execution_id="E4", task="t")
    assert r.status == "ABORTED"
    assert calls == ["LOOP_GUARD_EXCEEDED"]
    assert trace.events(event_type="DecisionEvent")


async def test_max_iterations_guard(tmp_path):
    async def decide(trigger, snapshot):
        return {"action": "ASK_USER", "target_node": None, "rationale": "cap"}

    # summary를 매번 다르게 해 same-finding이 아니라 iteration 한도(maxIterations=5)로 걸리게 한다
    fails = [dict(FAIL_REVIEW, summary=f"issue-{i}") for i in range(5)]
    engine, _, _ = make_engine(tmp_path, [PASS_DEV] * 5, fails, decide_fn=decide)
    r = await engine.run(execution_id="E5", task="t")
    assert r.status == "NEEDS_HUMAN"


async def test_token_budget_guard(tmp_path):
    # FakeAdapter는 turn당 output 1토큰 -> loop_policy를 낮춘 cfg 사본을 엔진에 주입한다
    calls = []

    async def decide(trigger, snapshot):
        calls.append(trigger)
        return {"action": "ASK_USER", "target_node": None, "rationale": "budget"}

    cfg = load_config()
    cfg = dataclasses.replace(cfg, loop_policy=LoopPolicy(999, 999, 2, 999))
    engine, _, _ = make_engine(tmp_path, [PASS_DEV, PASS_DEV], [FAIL_REVIEW, FAIL_REVIEW],
                               decide_fn=decide, cfg=cfg)
    r = await engine.run(execution_id="E5b", task="t")
    assert r.status == "NEEDS_HUMAN"
    assert calls == ["LOOP_GUARD_EXCEEDED"]


async def test_need_replan_goes_to_decision(tmp_path):
    async def decide(trigger, snapshot):
        assert snapshot["allowed_actions"] == ["REPLAN", "ASK_USER", "ABORT"]
        return {"action": "ASK_USER", "target_node": None, "rationale": "unclear"}

    engine, _, _ = make_engine(tmp_path, [{"status": "NEED_REPLAN", "summary": "?",
                                           "changed_files": [], "build": {"ok": False, "detail": ""},
                                           "tests": {"passed": 0, "failed": 0, "detail": ""}}],
                               decide_fn=decide)
    r = await engine.run(execution_id="E6", task="t")
    assert r.status == "NEEDS_HUMAN"


async def test_no_decide_fn_defaults_to_needs_human(tmp_path):
    engine, _, _ = make_engine(tmp_path, [{"status": "BLOCKED", "summary": "b",
                                           "changed_files": [], "build": {"ok": False, "detail": ""},
                                           "tests": {"passed": 0, "failed": 0, "detail": ""}}])
    r = await engine.run(execution_id="E7", task="t")
    assert r.status == "NEEDS_HUMAN"


async def test_classify_skips_conditional_nodes(tmp_path):
    # DEFAULT_TEMPLATE + explore 생략 결정 -> develop/review/qa만 실행
    async def decide(trigger, snapshot):
        assert trigger == "CLASSIFY"
        return {"action": "SKIP_NODE", "target_node": "explore", "rationale": "simple task"}

    # CLASSIFY는 1회 호출로 1개 노드만 생략 가능 -> qa는 남아 실행됨
    # develop과 qa는 둘 다 CLAUDE_CODE 어댑터를 쓰지만 각자 새 세션(로컬 카운터 0)이므로
    # 동일한 index-0 항목(GENERIC_PASS)을 각자 소비한다 — consume_result는 status만 본다.
    engine, _, _ = make_engine(tmp_path, [GENERIC_PASS], [PASS_REVIEW],
                               decide_fn=decide, template=DEFAULT_TEMPLATE)
    r = await engine.run(execution_id="E8", task="t")
    assert r.status == "COMPLETED"
    assert [h["node_id"] for h in r.node_history] == ["develop", "review", "qa"]


async def test_classify_invalid_skip_target_demotes_to_needs_human(tmp_path):
    # SKIP_NODE의 target이 conditional 노드가 아니면 invalid 결정 -> ASK_USER 강등
    # (main loop 진입 전에 종료되므로 어댑터 turn이 소비되지 않는다)
    async def decide(trigger, snapshot):
        return {"action": "SKIP_NODE", "target_node": "review", "rationale": "bad"}

    engine, _, _ = make_engine(tmp_path, decide_fn=decide, template=DEFAULT_TEMPLATE)
    r = await engine.run(execution_id="E8b", task="t")
    assert r.status == "NEEDS_HUMAN"


async def test_escalate_model_climbs_ladder_then_needs_human(tmp_path):
    # BLOCKED -> ESCALATE_MODEL -> DEFAULT의 다음 tier(HIGH_CAPABILITY)로 재실행.
    # 에스컬레이션된 노드도 새 세션(로컬 카운터 0부터)이므로 같은 index-0 항목을
    # 다시 읽는다 — 다시 BLOCKED가 나오면 사다리 끝(HIGH_CAPABILITY 다음 없음)이라
    # NEEDS_HUMAN으로 종료된다.
    blocked = {"status": "BLOCKED", "summary": "stuck", "changed_files": [],
              "build": {"ok": False, "detail": ""}, "tests": {"passed": 0, "failed": 0, "detail": ""}}
    calls = []

    async def decide(trigger, snapshot):
        calls.append(trigger)
        return {"action": "ESCALATE_MODEL", "target_node": None, "rationale": "retry bigger model"}

    engine, trace, _ = make_engine(tmp_path, [blocked], decide_fn=decide)
    r = await engine.run(execution_id="E9", task="t")
    assert r.status == "NEEDS_HUMAN"
    assert calls == ["BLOCKED", "BLOCKED"]
    esc = trace.events(event_type="ModelEscalationEvent")
    assert len(esc) == 1
    assert esc[0]["payload"]["reason"] == "BLOCKED"


async def test_retry_node_retries_current_node_then_completes(tmp_path):
    # BLOCKED -> RETRY_NODE(target 미지정 -> 현재 노드) -> 같은 세션에서 재시도 -> PASS 완주
    calls = []

    async def decide(trigger, snapshot):
        calls.append(trigger)
        return {"action": "RETRY_NODE", "target_node": None, "rationale": "try again"}

    engine, _, _ = make_engine(tmp_path, [BLOCKED_DEV, PASS_DEV], [PASS_REVIEW], decide_fn=decide)
    r = await engine.run(execution_id="E10", task="t")
    assert r.status == "COMPLETED"
    assert calls == ["BLOCKED"]
    assert [h["node_id"] for h in r.node_history] == ["develop", "develop", "review"]
    assert [h["transition"] for h in r.node_history] == ["BLOCKED", "PASS", "PASS"]


async def test_replan_jumps_to_target_and_resets_all_iterations(tmp_path):
    # develop PASS -> review NOT_PASS(develop iter=1) -> develop NEED_REPLAN ->
    # REPLAN(target=develop, 전 노드 iterations 리셋) -> develop PASS -> review NOT_PASS
    # (reset이 안 됐다면 develop iter가 2로 올라가 maxIterations=2에 걸려 즉시
    # LOOP_GUARD_EXCEEDED가 발생하고, 그 트리거에서 decide_fn이 ABORT를 반환하므로
    # 최종 status가 ABORTED가 돼 리셋 실패를 드러낸다) -> develop PASS -> review PASS
    cfg = load_config()
    cfg = dataclasses.replace(cfg, loop_policy=LoopPolicy(2, 999, 999999, 999))
    calls = []

    async def decide(trigger, snapshot):
        calls.append(trigger)
        if trigger == "NEED_REPLAN":
            return {"action": "REPLAN", "target_node": "develop", "rationale": "restart"}
        return {"action": "ABORT", "target_node": None, "rationale": "unexpected guard"}

    engine, _, _ = make_engine(
        tmp_path, [PASS_DEV, NEED_REPLAN_DEV, PASS_DEV, PASS_DEV],
        [FAIL_REVIEW, FAIL_REVIEW, PASS_REVIEW], decide_fn=decide, cfg=cfg)
    r = await engine.run(execution_id="E11", task="t")
    assert r.status == "COMPLETED"
    assert calls == ["NEED_REPLAN"]        # ABORT 분기(재-guard)를 타지 않았다 -> reset 성공
    assert [h["node_id"] for h in r.node_history] == \
        ["develop", "review", "develop", "develop", "review", "develop", "review"]
    # REPLAN 이후 재루프에서 develop iteration이 2(guard)가 아니라 1까지만 올라갔다
    # -> 전 노드 iterations 리셋이 실제로 적용됐다는 직접 증거.
    develop_iters = [h["iteration"] for h in r.node_history if h["node_id"] == "develop"]
    assert develop_iters == [0, 1, 0, 1]


async def test_classify_proceed_runs_all_nodes(tmp_path):
    # DEFAULT_TEMPLATE + CLASSIFY에서 PROCEED -> conditional 노드(explore)도 포함해 전부 실행
    async def decide(trigger, snapshot):
        assert trigger == "CLASSIFY"
        return {"action": "PROCEED", "target_node": None, "rationale": "full pipeline needed"}

    engine, _, _ = make_engine(tmp_path, [GENERIC_PASS], [PASS_REVIEW],
                               decide_fn=decide, template=DEFAULT_TEMPLATE)
    r = await engine.run(execution_id="E12", task="t")
    assert r.status == "COMPLETED"
    assert [h["node_id"] for h in r.node_history] == ["explore", "develop", "review", "qa"]


async def test_retry_node_unknown_target_demotes_to_needs_human(tmp_path):
    # RETRY_NODE의 target_node가 template에 없는 노드면 크래시 대신 NEEDS_HUMAN으로 강등
    async def decide(trigger, snapshot):
        return {"action": "RETRY_NODE", "target_node": "no-such-node", "rationale": "bad"}

    engine, _, _ = make_engine(tmp_path, [BLOCKED_DEV], decide_fn=decide)
    r = await engine.run(execution_id="E13", task="t")
    assert r.status == "NEEDS_HUMAN"


async def test_replan_unknown_target_demotes_to_needs_human(tmp_path):
    # REPLAN의 target_node가 template에 없는 노드면 크래시 대신 NEEDS_HUMAN으로 강등
    async def decide(trigger, snapshot):
        return {"action": "REPLAN", "target_node": "no-such-node", "rationale": "bad"}

    engine, _, _ = make_engine(tmp_path, [NEED_REPLAN_DEV], decide_fn=decide)
    r = await engine.run(execution_id="E14", task="t")
    assert r.status == "NEEDS_HUMAN"


async def test_workflow_engine_rejects_unknown_ladder_tier(tmp_path):
    # ESCALATION_LADDER의 tier가 cfg.tiers에 없으면 init에서 WorkflowError
    trace = TraceStore(tmp_path / "trace.db")
    registry = SessionRegistry(tmp_path / "harness.db")
    fake = FakeAdapter()
    orch = Orchestrator(trace, registry, {Provider.CLAUDE_CODE: fake, Provider.CODEX: fake})
    cfg = load_config()
    broken = dataclasses.replace(
        cfg, tiers={k: v for k, v in cfg.tiers.items() if k != "HIGH_CAPABILITY"})
    with pytest.raises(WorkflowError):
        WorkflowEngine(orch, broken, template=SIM)
