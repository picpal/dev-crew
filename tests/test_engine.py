import dataclasses

import pytest
from devcrew.adapters.base import FakeAdapter
from devcrew.config import LoopPolicy, load as load_config
from devcrew.engine import REPORT_MSG, REVISIT_MSG, ExecutionResult, WorkflowEngine
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
# EXPLORER/DEVELOPER/QA 세 role 모두의 output.schema.json required를 동시에 만족하는
# 범용 PASS fixture (finding #2 — consume_result가 이제 role bundle schema 전체의
# required 존재를 검사하므로, {"status":"PASS","summary":"ok"}만으로는 malformed로
# 강등돼 NEED_REPLAN이 된다). additionalProperties는 이 경량 검사 범위 밖이라 세
# role의 필드를 모두 포함해도 무방하다.
GENERIC_PASS = {"status": "PASS", "summary": "ok",
                "changed_files": [], "build": {"ok": True, "detail": ""},
                "tests": {"passed": 0, "failed": 0, "detail": ""},
                "plan": [], "results": [],
                "findings": [], "affected_files": []}
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


async def test_advance_revisit_sends_explicit_revisit_message(tmp_path):
    # 회귀 고정 (Task 4 R2 근본원인, task-7-report.md): review가 develop 루프백 이후
    # ADVANCE로 재진입할 때(세션은 이미 있음) None이 아니라 REVISIT_MSG가 실제로
    # adapter.send에 전달돼야 한다 — None이 새면 실 어댑터가 크래시한다.
    trace = TraceStore(tmp_path / "trace.db")
    registry = SessionRegistry(tmp_path / "harness.db")

    class RecordingAdapter(FakeAdapter):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.sent_messages: list[str | None] = []

        async def send(self, session_id, message):
            self.sent_messages.append(message)
            return await super().send(session_id, message)

    dev_fake = RecordingAdapter(structured_script=[PASS_DEV, PASS_DEV])
    review_fake = RecordingAdapter(structured_script=[FAIL_REVIEW, PASS_REVIEW])
    orch = Orchestrator(trace, registry,
                        {Provider.CLAUDE_CODE: dev_fake, Provider.CODEX: review_fake})
    engine = WorkflowEngine(orch, load_config(), template=SIM)
    r = await engine.run(execution_id="E15", task="t")
    assert r.status == "COMPLETED"
    # review의 1차 방문(신규 세션)은 REPORT_MSG, 2차 방문(ADVANCE 재진입)은 REVISIT_MSG
    assert review_fake.sent_messages == [REPORT_MSG, REVISIT_MSG]
    assert None not in review_fake.sent_messages
    assert None not in dev_fake.sent_messages


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
        return {"action": "ABORT", "target_node": None, "rationale": "test"}, None, 0

    engine, trace, _ = make_engine(tmp_path, [PASS_DEV] * 3, [FAIL_REVIEW] * 3, decide_fn=decide)
    r = await engine.run(execution_id="E4", task="t")
    assert r.status == "ABORTED"
    assert calls == ["LOOP_GUARD_EXCEEDED"]
    assert trace.events(event_type="DecisionEvent")


async def test_max_iterations_guard(tmp_path):
    async def decide(trigger, snapshot):
        return {"action": "ASK_USER", "target_node": None, "rationale": "cap"}, None, 0

    # summary와 findings를 매번 다르게 해(finding #1e: signature가 findings 있으면
    # findings를 우선한다) same-finding이 아니라 iteration 한도(maxIterations=5)로
    # 걸리게 한다
    fails = [dict(FAIL_REVIEW, summary=f"issue-{i}",
                  findings=[{**FAIL_REVIEW["findings"][0], "description": f"bug-{i}"}])
            for i in range(5)]
    engine, _, _ = make_engine(tmp_path, [PASS_DEV] * 5, fails, decide_fn=decide)
    r = await engine.run(execution_id="E5", task="t")
    assert r.status == "NEEDS_HUMAN"


async def test_token_budget_guard(tmp_path):
    # FakeAdapter는 turn당 output 1토큰 -> loop_policy를 낮춘 cfg 사본을 엔진에 주입한다
    calls = []

    async def decide(trigger, snapshot):
        calls.append(trigger)
        return {"action": "ASK_USER", "target_node": None, "rationale": "budget"}, None, 0

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
        return {"action": "ASK_USER", "target_node": None, "rationale": "unclear"}, None, 0

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
        return {"action": "SKIP_NODE", "target_node": "explore", "rationale": "simple task"}, None, 0

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
        return {"action": "SKIP_NODE", "target_node": "review", "rationale": "bad"}, None, 0

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
        return {"action": "ESCALATE_MODEL", "target_node": None, "rationale": "retry bigger model"}, None, 0

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
        return {"action": "RETRY_NODE", "target_node": None, "rationale": "try again"}, None, 0

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
    #
    # finding #1 수정 후 새 의미: 이 테스트는 "로컬 per-node iteration 카운터가
    # REPLAN으로 리셋된다"는 것만 검증한다 — 리셋되지 않는 실행 전체 누적 guard
    # (node_visits_total, replan_count)는 여기서 우연히도 트립되지 않도록 maxIterations를
    # 넉넉하게 잡는다(이 시나리오의 총 노드 방문 7회 << 10*2=20, REPLAN 적용도 1회
    # << MAX_ACTION_APPLICATIONS=2). "로컬은 리셋되지만 누적 guard는 유지된다"는
    # 사실 자체는 test_retry_node_capped_at_two_applications_then_demotes_to_needs_human/
    # test_replan_capped_at_two_applications_then_demotes_to_needs_human/
    # test_accumulated_node_visits_guard_overrides_advance_and_stops_after_two_triggers
    # 가 직접 재현한다(로컬 리셋에도 불구하고 결국 강제 종료됨을 보인다).
    cfg = load_config()
    cfg = dataclasses.replace(cfg, loop_policy=LoopPolicy(10, 999, 999999, 999))
    calls = []

    async def decide(trigger, snapshot):
        calls.append(trigger)
        if trigger == "NEED_REPLAN":
            return {"action": "REPLAN", "target_node": "develop", "rationale": "restart"}, None, 0
        return {"action": "ABORT", "target_node": None, "rationale": "unexpected guard"}, None, 0

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
        return {"action": "PROCEED", "target_node": None, "rationale": "full pipeline needed"}, None, 0

    engine, _, _ = make_engine(tmp_path, [GENERIC_PASS], [PASS_REVIEW],
                               decide_fn=decide, template=DEFAULT_TEMPLATE)
    r = await engine.run(execution_id="E12", task="t")
    assert r.status == "COMPLETED"
    assert [h["node_id"] for h in r.node_history] == ["explore", "develop", "review", "qa"]


async def test_retry_node_unknown_target_demotes_to_needs_human(tmp_path):
    # RETRY_NODE의 target_node가 template에 없는 노드면 크래시 대신 NEEDS_HUMAN으로 강등
    async def decide(trigger, snapshot):
        return {"action": "RETRY_NODE", "target_node": "no-such-node", "rationale": "bad"}, None, 0

    engine, _, _ = make_engine(tmp_path, [BLOCKED_DEV], decide_fn=decide)
    r = await engine.run(execution_id="E13", task="t")
    assert r.status == "NEEDS_HUMAN"


async def test_replan_unknown_target_demotes_to_needs_human(tmp_path):
    # REPLAN의 target_node가 template에 없는 노드면 크래시 대신 NEEDS_HUMAN으로 강등
    async def decide(trigger, snapshot):
        return {"action": "REPLAN", "target_node": "no-such-node", "rationale": "bad"}, None, 0

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


async def test_retry_node_capped_at_two_applications_then_demotes_to_needs_human(tmp_path):
    # 원 codex 리뷰 재현의 직접 회귀(finding #1): decide_fn이 BLOCKED마다 항상
    # RETRY_NODE를 반환해도(로컬 iteration을 매번 0으로 리셋) 실행 전체 누적
    # retry_count가 MAX_ACTION_APPLICATIONS(2)를 넘으면 3번째부터 ASK_USER로 강등돼
    # 종료한다 — 수정 전에는 이 시나리오가 8회까지 반복됐다.
    calls = []

    async def decide(trigger, snapshot):
        calls.append(trigger)
        return {"action": "RETRY_NODE", "target_node": None, "rationale": "retry"}, None, 0

    engine, _, _ = make_engine(
        tmp_path, [BLOCKED_DEV, BLOCKED_DEV, BLOCKED_DEV], decide_fn=decide)
    r = await engine.run(execution_id="E17", task="t")
    assert r.status == "NEEDS_HUMAN"
    assert calls == ["BLOCKED", "BLOCKED", "BLOCKED"]      # 8회가 아니라 3회에서 종료
    assert [h["node_id"] for h in r.node_history] == ["develop", "develop", "develop"]


async def test_replan_capped_at_two_applications_then_demotes_to_needs_human(tmp_path):
    # REPLAN도 RETRY_NODE와 대칭으로 실행 전체 누적 replan_count가 2회를 넘으면
    # 3번째부터 ASK_USER로 강등한다 (전 노드 iterations 리셋으로 이 guard를 우회할
    # 수 없다).
    calls = []

    async def decide(trigger, snapshot):
        calls.append(trigger)
        return {"action": "REPLAN", "target_node": "develop", "rationale": "restart"}, None, 0

    engine, _, _ = make_engine(
        tmp_path, [NEED_REPLAN_DEV, NEED_REPLAN_DEV, NEED_REPLAN_DEV], decide_fn=decide)
    r = await engine.run(execution_id="E18", task="t")
    assert r.status == "NEEDS_HUMAN"
    assert calls == ["NEED_REPLAN", "NEED_REPLAN", "NEED_REPLAN"]
    assert [h["node_id"] for h in r.node_history] == ["develop", "develop", "develop"]


async def test_accumulated_node_visits_guard_overrides_advance_and_stops_after_two_triggers(tmp_path):
    # finding #1c/#1d 회귀: maxIterations=1인 2-노드 템플릿에서 첫 LOOP_GUARD_EXCEEDED는
    # 로컬 per-node iteration guard로 트립되고(develop, 1회 loop-back), decide_fn이
    # 매번 REPLAN을 반환해 로컬 카운터를 리셋해도 실행 전체 누적 node_visits_total은
    # 리셋되지 않아 그 다음 노드 완주(ADVANCE)에서 누적 guard가 다시 트립된다 —
    # 토큰/duration guard처럼 LOOP 분기 밖(ADVANCE)에서도 검사됨을 보인다. 3번째
    # LOOP_GUARD_EXCEEDED는 (loop_guard_trigger_count>2) 결정 없이 즉시 NEEDS_HUMAN.
    cfg = load_config()
    cfg = dataclasses.replace(cfg, loop_policy=LoopPolicy(1, 999, 999999, 999))
    calls = []

    async def decide(trigger, snapshot):
        calls.append(trigger)
        return {"action": "REPLAN", "target_node": "develop", "rationale": "retry"}, None, 0

    engine, _, _ = make_engine(
        tmp_path, [PASS_DEV, PASS_DEV, PASS_DEV], [FAIL_REVIEW], decide_fn=decide, cfg=cfg)
    r = await engine.run(execution_id="E19", task="t")
    assert r.status == "NEEDS_HUMAN"
    # 3번째 LOOP_GUARD_EXCEEDED는 decide_fn을 부르지 않는다 -> calls는 2개뿐
    assert calls == ["LOOP_GUARD_EXCEEDED", "LOOP_GUARD_EXCEEDED"]


async def test_decide_fn_exception_demotes_to_needs_human(tmp_path):
    # finding #4 회귀: decide_fn이 예외를 내도 엔진 경계에서 ASK_USER로 강등한다
    # (예외가 그대로 누출돼 run()을 죽이지 않는다).
    async def decide(trigger, snapshot):
        raise RuntimeError("boom")

    engine, _, _ = make_engine(tmp_path, [BLOCKED_DEV], decide_fn=decide)
    r = await engine.run(execution_id="E20", task="t")
    assert r.status == "NEEDS_HUMAN"


async def test_decide_fn_returns_none_demotes_to_needs_human(tmp_path):
    # finding #4 회귀: decide_fn이 None을 반환해도 (예: 구현 버그) AttributeError 없이
    # ASK_USER로 강등한다 — 원래는 engine.py의 `.get()` 호출에서 크래시했다.
    async def decide(trigger, snapshot):
        return None

    engine, _, _ = make_engine(tmp_path, [BLOCKED_DEV], decide_fn=decide)
    r = await engine.run(execution_id="E21", task="t")
    assert r.status == "NEEDS_HUMAN"


async def test_decide_fn_returns_bare_dict_demotes_to_needs_human(tmp_path):
    # finding #4/#5 회귀: decide_fn이 새 3-tuple 계약 (decision, producer_id,
    # usage_tokens)을 지키지 않고 dict만 반환하면(구 계약의 잔재) ASK_USER로 강등한다.
    async def decide(trigger, snapshot):
        return {"action": "ABORT", "target_node": None, "rationale": "old contract"}

    engine, _, _ = make_engine(tmp_path, [BLOCKED_DEV], decide_fn=decide)
    r = await engine.run(execution_id="E22", task="t")
    assert r.status == "NEEDS_HUMAN"


async def test_decision_event_records_raw_applied_degraded_and_producer(tmp_path):
    # finding #5 회귀: DecisionEvent는 trigger worker나 None이 아니라 결정을 생산한
    # instance(producer_id)에 귀속되고, 검증 전 원본(raw)과 검증/강등 후 실제 적용된
    # 결정(applied), 강등 여부(degraded)를 함께 기록한다. decide_fn이 보고한
    # usage_tokens도 실행 전체 토큰에 합산된다(finding #6b).
    async def decide(trigger, snapshot):
        # BLOCKED에는 PROCEED가 허용되지 않는다 -> engine이 ASK_USER로 강등해야 한다
        return {"action": "PROCEED", "target_node": None, "rationale": "bad"}, "ORC-test", 7

    engine, trace, _ = make_engine(tmp_path, [BLOCKED_DEV], decide_fn=decide)
    r = await engine.run(execution_id="E23", task="t")
    assert r.status == "NEEDS_HUMAN"
    evs = trace.events(event_type="DecisionEvent")
    assert evs
    payload = evs[-1]["payload"]
    assert payload["raw"]["action"] == "PROCEED"
    assert payload["applied"]["action"] == "ASK_USER"
    assert payload["degraded"] is True
    assert evs[-1]["instance_id"] == "ORC-test"
    assert r.total_tokens >= 7     # decide_fn이 보고한 usage_tokens가 합산됐다


async def test_loop_event_recorded_on_every_loop_transition(tmp_path):
    # finding #5 회귀: LOOP 전이가 발생할 때마다(guard 초과 여부와 무관하게) 그
    # 전이를 만든(생산자) instance의 LoopEvent를 남긴다.
    engine, trace, _ = make_engine(tmp_path, [PASS_DEV, PASS_DEV], [FAIL_REVIEW, PASS_REVIEW])
    r = await engine.run(execution_id="E24", task="t")
    assert r.status == "COMPLETED"
    loop_events = trace.events(event_type="LoopEvent")
    assert len(loop_events) == 1
    assert loop_events[0]["payload"]["node_id"] == "review"
    assert loop_events[0]["payload"]["transition"] == "NOT_PASS"
    assert loop_events[0]["instance_id"].startswith("REV")
