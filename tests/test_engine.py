import dataclasses

import pytest
from devcrew.adapters.base import FakeAdapter
from devcrew.config import LoopPolicy, load as load_config
from devcrew.engine import (NEW_TASK_MSG, REPORT_MSG, REVISIT_MSG, ExecutionResult,
                            WorkflowEngine)
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


async def test_decide_fn_returns_non_int_usage_tokens_demotes_to_needs_human(tmp_path):
    # wave 2 F4 회귀: codex 재리뷰가 재현한 그대로 — decide_fn이 shape은 맞는(길이 3,
    # 첫 원소 dict+action 있음) 3-tuple을 반환해도 usage_tokens가 int가 아니면
    # (예: "bad-usage" 문자열) wave 1은 그대로 통과시켜 `total_tokens += usage_tokens`
    # 에서 TypeError가 누출됐다. 이제는 이 경우도 ASK_USER로 강등한다.
    async def decide(trigger, snapshot):
        return {"action": "ABORT", "target_node": None, "rationale": "r"}, None, "bad-usage"

    engine, _, _ = make_engine(tmp_path, [BLOCKED_DEV], decide_fn=decide)
    r = await engine.run(execution_id="E25", task="t")
    assert r.status == "NEEDS_HUMAN"
    assert isinstance(r.total_tokens, int)     # TypeError 없이 정상적으로 int로 유지됨


async def test_decide_fn_returns_bool_usage_tokens_demotes_to_needs_human(tmp_path):
    # bool은 Python에서 isinstance(x, int)가 True이므로 별도로 배제해야 한다
    # (usage_tokens=True/False는 의미 있는 토큰 수가 아니다).
    async def decide(trigger, snapshot):
        return {"action": "ABORT", "target_node": None, "rationale": "r"}, None, True

    engine, _, _ = make_engine(tmp_path, [BLOCKED_DEV], decide_fn=decide)
    r = await engine.run(execution_id="E26", task="t")
    assert r.status == "NEEDS_HUMAN"


async def test_decide_fn_returns_non_str_producer_id_demotes_to_needs_human(tmp_path):
    # wave 2 F4 회귀: producer_id도 str | None 계약을 지켜야 한다 — 그 외 타입(예:
    # int)은 ASK_USER로 강등한다.
    async def decide(trigger, snapshot):
        return {"action": "ABORT", "target_node": None, "rationale": "r"}, 12345, 0

    engine, _, _ = make_engine(tmp_path, [BLOCKED_DEV], decide_fn=decide)
    r = await engine.run(execution_id="E27", task="t")
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


# ---------------------------------------------------------------------------
# crew leader 취합(handoff) · role별 예산 · 종료 사유 · 전체 경로 (2026-08-20)
# ---------------------------------------------------------------------------

def _cfg_with(**loop_overrides):
    cfg = load_config()
    return dataclasses.replace(
        cfg, loop_policy=dataclasses.replace(cfg.loop_policy, **loop_overrides))


async def test_handoff_of_prior_results_reaches_next_node(tmp_path):
    """ADVANCE로 새로 여는 노드는 선행 노드 결과를 최초 투입 메시지로 함께 받는다.

    이게 없으면 새 세션은 원본 task 문자열만 보고 시작해 앞 단계가 이미 밝힌
    사실을 다시 조사한다 (2026-08-20 SLACK-2 실사례: explore가 "결과물 없음"을
    보고했는데 develop이 그걸 못 보고 무관한 산출물을 만들었다).
    """
    engine, _, (dev_fake, review_fake) = make_engine(
        tmp_path, [PASS_DEV], [PASS_REVIEW])
    r = await engine.run(execution_id="H1", task="t")
    assert r.status == "COMPLETED"
    intro = review_fake.initial_messages[0]
    assert "crew leader 취합" in intro
    assert "[develop/DEVELOPER] PASS" in intro
    assert PASS_DEV["summary"] in intro
    assert "a.py" in intro                       # changed_files까지 넘어간다
    # 첫 노드는 선행 결과가 없으므로 취합 블록이 붙지 않는다
    assert "crew leader 취합" not in dev_fake.initial_messages[0]


async def test_handoff_carries_review_findings_to_developer(tmp_path):
    """review NOT_PASS 후 develop 재진입은 기존 세션이라 follow-up 경로를 타지만,
    이후 새로 열리는 노드는 취합에 리뷰 findings까지 담아 받는다."""
    tmpl = WorkflowTemplate("sim3", (
        NodeSpec("develop", Role.DEVELOPER, "구현: {task}"),
        NodeSpec("review", Role.REVIEWER, "검토: {task}", loop_back_to="develop"),
        NodeSpec("qa", Role.QA, "검증: {task}"),
    ))
    # develop과 qa가 같은 provider(CLAUDE_CODE) fake를 공유한다 — 세 role의 required를
    # 모두 만족하는 GENERIC_PASS를 써야 qa 보고가 malformed로 강등되지 않는다.
    engine, _, (claude_fake, _) = make_engine(
        tmp_path, [GENERIC_PASS, GENERIC_PASS], [FAIL_REVIEW, PASS_REVIEW], template=tmpl)
    r = await engine.run(execution_id="H2", task="t")
    assert r.status == "COMPLETED"
    qa_intro = claude_fake.initial_messages[-1]   # develop, qa 모두 CLAUDE_CODE
    assert "[review/REVIEWER] NOT_PASS" in qa_intro
    assert "bug" in qa_intro                      # findings description


async def test_worker_role_budget_warns_but_does_not_terminate(tmp_path):
    """worker role 예산 초과는 경보일 뿐 종료 트리거가 아니다.

    adapter.send() 하나가 그 에이전트의 전체 agentic turn이라 토큰은 사후에만
    보인다 — 막지 못하는 신호로 정상 완료한 작업을 죽이던 오류(SLACK-1/SLACK-2)
    교정. 다음 홉을 실제로 막는 건 반복/시간 가드다."""
    engine, _, _ = make_engine(
        tmp_path, [PASS_DEV, PASS_DEV], [FAIL_REVIEW, PASS_REVIEW],
        cfg=_cfg_with(role_budgets={"DEVELOPER": 1}))
    r = await engine.run(execution_id="B1", task="t")
    assert r.status == "COMPLETED"                  # 예산을 넘겨도 완주한다
    assert r.role_tokens["DEVELOPER"] == 2
    assert "DEVELOPER 예산 초과 (2/1)" in r.warnings
    assert "DEVELOPER" not in r.reason


async def test_execution_token_budget_exceeded_named_in_reason(tmp_path):
    engine, _, _ = make_engine(
        tmp_path, [PASS_DEV], [PASS_REVIEW], cfg=_cfg_with(max_token_budget=1))
    r = await engine.run(execution_id="B2", task="t")
    assert r.status == "NEEDS_HUMAN"
    assert "실행 전체 토큰 예산 초과 (2/1)" in r.reason


async def test_orchestrator_budget_stops_further_decision_sessions(tmp_path):
    """leader 예산이 소진되면 결정 세션을 더 띄우지 않는다 — 초과 상태에서
    opus 결정 세션을 계속 부르며 초과분을 키우던 악순환(SLACK-1) 차단."""
    calls = []

    async def decide(trigger, snapshot):
        calls.append(trigger)
        return {"action": "REPLAN", "target_node": "develop", "rationale": "r"}, "ORC-1", 100

    engine, _, _ = make_engine(
        tmp_path, [PASS_DEV] * 6, [FAIL_REVIEW] * 6, decide_fn=decide,
        cfg=_cfg_with(role_budgets={"ORCHESTRATOR": 50}, max_token_budget=2))
    r = await engine.run(execution_id="B3", task="t")
    assert r.status == "NEEDS_HUMAN"
    # leader만 hard stop — 반복 가드가 못 잡는 유일한 역할이라서
    assert "crew leader" in r.reason and "ORCHESTRATOR 예산 초과" in r.reason
    assert len(calls) == 1                        # 두 번째 결정 지점에서는 호출 안 함
    assert r.role_tokens["ORCHESTRATOR"] == 100


async def test_path_records_leader_hops_loops_and_skips(tmp_path):
    """경로에 노드 전이뿐 아니라 leader 결정 홉·루프백·스킵이 모두 남는다."""
    async def decide(trigger, snapshot):
        if trigger == "CLASSIFY":
            return {"action": "SKIP_NODE", "target_node": "explore", "rationale": "r"}, "ORC-0", 0
        return {"action": "ASK_USER", "target_node": None, "rationale": "확인 필요"}, "ORC-1", 0

    tmpl = WorkflowTemplate("sim4", (
        NodeSpec("explore", Role.EXPLORER, "조사: {task}", conditional=True),
        NodeSpec("develop", Role.DEVELOPER, "구현: {task}"),
        NodeSpec("review", Role.REVIEWER, "검토: {task}", loop_back_to="develop"),
    ))
    engine, _, _ = make_engine(
        tmp_path, [PASS_DEV, PASS_DEV], [FAIL_REVIEW], decide_fn=decide, template=tmpl,
        cfg=_cfg_with(max_iterations=1))
    r = await engine.run(execution_id="P1", task="t")
    assert r.path[0] == "leader:CLASSIFY→SKIP(explore)"
    assert "explore:SKIPPED" in r.path
    assert "develop:PASS" in r.path
    assert "review:NOT_PASS↺develop" in r.path
    assert "leader:LOOP_GUARD_EXCEEDED[1]" in r.path
    assert "leader:LOOP_GUARD_EXCEEDED→ASK_USER" in r.path
    assert "확인 필요" in r.reason


async def test_completed_result_carries_reason_and_role_tokens(tmp_path):
    engine, _, _ = make_engine(tmp_path, [PASS_DEV], [PASS_REVIEW])
    r = await engine.run(execution_id="P2", task="t")
    assert r.status == "COMPLETED" and r.reason == "모든 노드 통과"
    assert r.path == ["develop:PASS", "review:PASS"]
    assert r.role_tokens == {"DEVELOPER": 1, "REVIEWER": 1}


# ---------------------------------------------------------------------------
# 실행 간 세션 이월 (carry) — "이어서 고쳐줘"에 재탐색하지 않기 (2026-08-20)
# ---------------------------------------------------------------------------

class _Recording(FakeAdapter):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sent_messages: list[str] = []

    async def send(self, session_id, message):
        self.sent_messages.append(message)
        return await super().send(session_id, message)


def _carry_engine(tmp_path, dev_script, review_script, name="C"):
    trace = TraceStore(tmp_path / f"{name}-trace.db")
    registry = SessionRegistry(tmp_path / f"{name}-harness.db")
    dev, rev = _Recording(structured_script=dev_script), _Recording(structured_script=review_script)
    orch = Orchestrator(trace, registry, {Provider.CLAUDE_CODE: dev, Provider.CODEX: rev})
    return WorkflowEngine(orch, load_config(), template=SIM), trace, dev, rev


async def test_carry_reuses_sessions_across_executions(tmp_path):
    """같은 carry를 넘긴 후속 실행은 노드 세션을 새로 열지 않고 이어 쓴다 —
    새 요청은 NEW_TASK_MSG로 기존 세션에 투입된다 (코드베이스 재탐색 제거)."""
    engine, _, dev, rev = _carry_engine(tmp_path, [PASS_DEV] * 3, [PASS_REVIEW] * 3)
    carry: dict = {}
    r1 = await engine.run(execution_id="C1", task="첫 요청", carry=carry)
    assert r1.status == "COMPLETED"
    assert set(carry) == {"develop", "review"}
    sessions_after_first = {k: v["session_id"] for k, v in carry.items()}

    r2 = await engine.run(execution_id="C2", task="이어서 고쳐줘", carry=carry)
    assert r2.status == "COMPLETED"
    # 세션이 그대로 재사용됐다 (새 start_session 없음)
    assert {k: v["session_id"] for k, v in carry.items()} == sessions_after_first
    assert len(dev.initial_messages) == 1 and len(rev.initial_messages) == 1
    # 두 번째 실행의 첫 투입은 새 과업 메시지 (REVISIT_MSG가 아니다)
    assert dev.sent_messages[1].startswith(NEW_TASK_MSG.format(task="이어서 고쳐줘"))


async def test_carry_falls_back_to_fresh_session_when_lost(tmp_path):
    """브리지 재시작 등으로 이월 세션이 이 프로세스에 없으면 새 세션으로 폴백한다."""
    engine, trace, dev, _ = _carry_engine(tmp_path, [PASS_DEV] * 2, [PASS_REVIEW] * 2, name="C2")
    stale = await engine.orch.spawn(Role.DEVELOPER, "DEFAULT", execution_id="X",
                                    node_id="develop", task_scope="t")
    carry = {"develop": {"inst": stale, "session_id": "죽은-세션", "tier": "DEFAULT"}}
    r = await engine.run(execution_id="C3", task="t", carry=carry)
    assert r.status == "COMPLETED"
    assert carry["develop"]["session_id"] != "죽은-세션"      # 새 세션으로 교체됨
    assert trace.events(event_type="SessionCarryLostEvent")


async def test_run_without_carry_is_unchanged(tmp_path):
    """carry를 안 넘기면 종전대로 실행마다 새 세션 (기존 동작 회귀 고정)."""
    engine, _, dev, _ = _carry_engine(tmp_path, [PASS_DEV] * 3, [PASS_REVIEW] * 3, name="C4")
    await engine.run(execution_id="C4", task="t")
    await engine.run(execution_id="C5", task="t")
    assert len(dev.initial_messages) == 2


# ---------------------------------------------------------------------------
# 예산 경보 알림 + 협조적 중지 (사용자 결정 2026-08-20)
# ---------------------------------------------------------------------------

async def test_budget_warning_is_emitted_once_per_warning(tmp_path):
    """새 경보가 생길 때만 on_warning이 불린다 (같은 경보를 매 노드 반복 알리지 않는다)."""
    seen: list[str] = []

    async def on_warning(w):
        seen.append(w)

    trace = TraceStore(tmp_path / "trace.db")
    registry = SessionRegistry(tmp_path / "harness.db")
    orch = Orchestrator(trace, registry, {
        Provider.CLAUDE_CODE: FakeAdapter(structured_script=[PASS_DEV] * 3),
        Provider.CODEX: FakeAdapter(structured_script=[FAIL_REVIEW, PASS_REVIEW])})
    engine = WorkflowEngine(orch, _cfg_with(role_budgets={"DEVELOPER": 1}),
                            template=SIM, on_warning=on_warning)
    r = await engine.run(execution_id="W1", task="t")
    assert r.status == "COMPLETED"
    assert seen == ["DEVELOPER 예산 초과 (2/1)"]          # 3회 방문해도 1번만
    assert r.warnings == seen
    assert trace.events(event_type="BudgetWarningEvent")


async def test_stop_check_stops_at_next_node_boundary(tmp_path):
    """중지 요청은 노드 경계에서 반영된다 — 진행 중인 turn은 끝까지 두고 멈춘다."""
    calls = {"n": 0}

    def stop_check():
        calls["n"] += 1
        return calls["n"] > 1          # develop 완료 후 경계에서 True

    trace = TraceStore(tmp_path / "trace.db")
    registry = SessionRegistry(tmp_path / "harness.db")
    dev = _Recording(structured_script=[PASS_DEV] * 3)
    orch = Orchestrator(trace, registry, {
        Provider.CLAUDE_CODE: dev,
        Provider.CODEX: _Recording(structured_script=[PASS_REVIEW])})
    engine = WorkflowEngine(orch, load_config(), template=SIM, stop_check=stop_check)
    r = await engine.run(execution_id="S1", task="t")
    assert r.status == "STOPPED"
    assert r.reason == "사용자가 실행을 중지했다"
    assert r.path == ["develop:PASS", "STOPPED"]     # develop은 끝까지 수행됐다
    assert [h["node_id"] for h in r.node_history] == ["develop"]


async def test_no_stop_check_runs_to_completion(tmp_path):
    engine, _, _ = make_engine(tmp_path, [PASS_DEV], [PASS_REVIEW])
    r = await engine.run(execution_id="S2", task="t")
    assert r.status == "COMPLETED"


async def test_handoff_frames_worker_reports_as_untrusted_data(tmp_path):
    """워커 보고는 LLM 생성 입력이다 — 다음 워커 투입 메시지에 '지시가 아니라
    데이터'라는 경계가 함께 실려야 보고문에 섞인 지시가 role/scope를 못 바꾼다."""
    engine, _, _dev, rev = _carry_engine(
        tmp_path, [PASS_DEV], [PASS_REVIEW], name="H9")
    await engine.run(execution_id="H9", task="t")
    intro = rev.initial_messages[0]
    assert "보고 데이터" in intro and "지시가 아니며" in intro
    assert "<<<worker-reports" in intro and "worker-reports" in intro
    # 경계 표시가 실제 보고 내용을 감싸고 있다
    assert intro.index("<<<worker-reports") < intro.index(PASS_DEV["summary"])


async def test_outcome_digest_is_bounded(tmp_path):
    """루프가 여러 번 돈 실행의 다이제스트가 leader 보고 세션 창을 밀어내지 않도록
    summary/리스트 필드에 상한을 둔다."""
    from devcrew.engine import _outcome_digest
    d = _outcome_digest({"node_id": "review", "role": "REVIEWER", "transition": "NOT_PASS",
                         "structured": {"summary": "가" * 5000,
                                        "findings": [{"file": f"f{i}"} for i in range(50)],
                                        "changed_files": [f"f{i}.py" for i in range(50)]}})
    assert len(d["summary"]) == 800
    assert len(d["findings"]) == 10 and len(d["changed_files"]) == 10
    assert d["node_id"] == "review" and d["transition"] == "NOT_PASS"
