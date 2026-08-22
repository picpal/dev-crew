import dataclasses

import pytest
from devcrew.adapters.base import FakeAdapter
from devcrew.config import load as load_config
from devcrew.decision import (LEADER_INTRO, DecisionError, make_llm_decide,
                              validate_decision)
from devcrew.orchestrator import Orchestrator
from devcrew.schema import Provider, Role, Usage
from devcrew.store.registry import SessionRegistry
from devcrew.store.trace import TraceStore
from devcrew.workflow import DEFAULT_TEMPLATE

# report는 strict schema상 required(nullable) — 결정 turn에서는 null이다
GOOD = {"status": "PASS", "summary": "s", "report": None,
        "decision": {"action": "ABORT", "target_node": None, "rationale": "r"}}


def test_validate_decision_ok():
    d = validate_decision(GOOD, "NEED_REPLAN", DEFAULT_TEMPLATE)
    assert d["action"] == "ABORT"


@pytest.mark.parametrize("bad", [
    None,                                                        # malformed
    {"status": "PASS", "summary": "s", "report": None},           # decision 없음
    {**GOOD, "decision": {"action": "PROCEED", "target_node": None, "rationale": "r"}},  # 허용 밖 action
    {**GOOD, "decision": {"action": "REPLAN", "target_node": "nope", "rationale": "r"}}, # unknown node
    {**GOOD, "status": "BLOCKED"},                               # 결정 미수행
])
def test_validate_decision_rejects(bad):
    with pytest.raises(DecisionError):
        validate_decision(bad, "NEED_REPLAN", DEFAULT_TEMPLATE)


def make_env(tmp_path, structured_script):
    trace = TraceStore(tmp_path / "trace.db")
    registry = SessionRegistry(tmp_path / "harness.db")
    fake = FakeAdapter(structured_script=structured_script)
    orch = Orchestrator(trace, registry,
                        {Provider.CLAUDE_CODE: fake, Provider.CODEX: fake})
    cfg = load_config()
    return orch, trace, fake, cfg


async def test_llm_decide_retries_once_then_degrades(tmp_path):
    # 1차: 허용 밖 action → 재시도 메시지 전송, 2차: 여전히 invalid → ASK_USER 강등
    invalid = {"status": "PASS", "summary": "s", "report": None,
              "decision": {"action": "PROCEED", "target_node": None, "rationale": "r"}}
    orch, trace, fake, cfg = make_env(tmp_path, [invalid, invalid])
    decide = make_llm_decide(orch, cfg)
    snapshot = {"execution_id": "E1"}
    d, producer_id, usage_tokens = await decide("NEED_REPLAN", snapshot)
    assert d["action"] == "ASK_USER"
    assert producer_id is not None                 # finding #5: 생산자 ORCHESTRATOR instance_id
    assert fake.turns                              # 재시도로 send 2회 소비됨을 확인
    assert sum(fake.turns.values()) == 2
    assert usage_tokens == 2                        # finding #6b: 두 turn(1차+재시도)의 usage 합계


async def test_llm_decide_happy_path_spawns_orchestrator(tmp_path):
    orch, trace, fake, cfg = make_env(tmp_path, [GOOD])
    decide = make_llm_decide(orch, cfg, mcp_servers={"harness": "sentinel"})
    snapshot = {"execution_id": "E2"}
    d, producer_id, usage_tokens = await decide("NEED_REPLAN", snapshot)
    assert d["action"] == "ABORT"
    assert usage_tokens == 1                        # finding #6b: 단일 turn(재시도 없음)의 usage

    routing = trace.events(event_type="ModelRoutingEvent")
    orch_routing = [e for e in routing if e["payload"]["role"] == "ORCHESTRATOR"]
    assert orch_routing and orch_routing[0]["payload"]["selected_tier"] == "HIGH_CAPABILITY"

    instance_events = trace.events(event_type="InstanceEvent")
    assert any(e["payload"]["role"] == "ORCHESTRATOR" for e in instance_events)
    # finding #5: producer_id는 이 결정을 낸 fresh ORCHESTRATOR instance와 일치한다
    assert any(e["payload"]["role"] == "ORCHESTRATOR" and producer_id.startswith("ORC")
              for e in instance_events)

    assert fake.last_mcp_servers == {"harness": "sentinel"}


# ---------------------------------------------------------------------------
# crew leader 컨텍스트 유지 + compaction (사용자 결정 2026-08-20)
# ---------------------------------------------------------------------------

SNAP = {"execution_id": "E1", "task": "t", "trigger": "NEED_REPLAN",
        "allowed_actions": ["REPLAN", "ASK_USER", "ABORT"]}


async def test_leader_state_keeps_one_session_across_decisions(tmp_path):
    """leader_state를 넘기면 결정마다 새 세션을 띄우지 않고 한 세션을 이어 쓴다."""
    orch, _, fake, cfg = make_env(tmp_path, [GOOD] * 10)
    state: dict = {}
    decide = make_llm_decide(orch, cfg, leader_state=state)
    _, id1, _ = await decide("NEED_REPLAN", SNAP)
    _, id2, _ = await decide("NEED_REPLAN", SNAP)
    assert id1 == id2                                   # 같은 leader instance
    assert len(fake.initial_messages) == 1              # 세션은 하나뿐
    assert LEADER_INTRO in fake.initial_messages[0]
    assert state["sid"] and state["inst"] is not None


async def test_without_leader_state_each_decision_is_fresh(tmp_path):
    """기존 동작 회귀 고정 — leader_state 없이는 결정마다 fresh 세션."""
    orch, _, fake, cfg = make_env(tmp_path, [GOOD] * 10)
    decide = make_llm_decide(orch, cfg)
    _, id1, _ = await decide("NEED_REPLAN", SNAP)
    _, id2, _ = await decide("NEED_REPLAN", SNAP)
    assert id1 != id2
    assert len(fake.initial_messages) == 2
    assert LEADER_INTRO not in fake.initial_messages[0]


async def test_leader_compacts_when_context_window_fills(tmp_path):
    """컨텍스트 점유가 임계치에 닿으면 leader가 스스로 요약하고 그 요약만 들고
    새 세션으로 넘어간다 — 창은 비우되 결정 맥락은 유지."""
    class BigContext(FakeAdapter):
        async def send(self, session_id, message):
            out = await super().send(session_id, message)
            return dataclasses.replace(out, usage=Usage(
                input_tokens=100, output_tokens=10, cache_read_input_tokens=900))

    trace = TraceStore(tmp_path / "trace.db")
    registry = SessionRegistry(tmp_path / "harness.db")
    summary_turn = {**GOOD, "summary": "지금까지 확정: A안 채택"}
    fake = BigContext(structured_script=[GOOD, summary_turn, GOOD, GOOD])
    orch = Orchestrator(trace, registry,
                        {Provider.CLAUDE_CODE: fake, Provider.CODEX: fake})
    state: dict = {}
    decide = make_llm_decide(orch, load_config(), leader_state=state, compact_at=1000)

    _, id1, _ = await decide("NEED_REPLAN", SNAP)
    assert state["context_used"] == 1010               # 100 + 10 + 900
    _, id2, _ = await decide("NEED_REPLAN", SNAP)

    assert id1 != id2                                   # 세션이 교체됐다
    assert len(fake.initial_messages) == 2
    seed_intro = fake.initial_messages[1]
    assert "[이전 컨텍스트 요약" in seed_intro
    assert "지금까지 확정: A안 채택" in seed_intro
    assert trace.events(event_type="LeaderCompactEvent")
    assert state["context_used"] == 1010                # 새 세션의 첫 turn 기준


async def test_final_report_written_by_leader(tmp_path):
    """실행 결과를 leader가 사용자 언어로 쓴 글이 보고문이 된다."""
    from devcrew.decision import make_final_report
    written = {**GOOD, "report": "*완료* — `calc.py`에 `mul()`을 추가했습니다."}
    orch, _, fake, _ = make_env(tmp_path, [GOOD, written])
    state: dict = {}
    decide = make_llm_decide(orch, load_config(), leader_state=state)
    await decide("NEED_REPLAN", SNAP)

    text, spent = await make_final_report(orch, state)({"status": "COMPLETED"})
    assert text == "*완료* — `calc.py`에 `mul()`을 추가했습니다."
    assert spent > 0
    assert len(fake.initial_messages) == 1              # 보고도 같은 leader 세션에서


async def test_final_report_returns_none_without_leader_session(tmp_path):
    """leader 세션이 없으면(비지속 모드) 보고문 없이 기계 요약만 나간다."""
    from devcrew.decision import make_final_report
    orch, _, _, _ = make_env(tmp_path, [GOOD])
    assert await make_final_report(orch, None)({"status": "COMPLETED"}) == (None, 0)
    assert await make_final_report(orch, {})({"status": "COMPLETED"}) == (None, 0)


async def test_compaction_uses_adapter_measurement(tmp_path):
    """압축 시점도 추정이 아니라 어댑터 실측 점유율(`/context`)로 판단한다."""
    orch, trace, fake, cfg = make_env(tmp_path, [GOOD, {**GOOD, "summary": "요약"}, GOOD])
    fake.context = {"used": 610_000, "window": 1_000_000, "pct": 61.0, "source": "sdk"}
    state: dict = {}
    # compact_at(추정 임계)은 절대 안 닿는 값 — 실측만이 압축을 부를 수 있다
    decide = make_llm_decide(orch, cfg, leader_state=state,
                             compact_at=10 ** 9, compact_ratio=0.5)
    _, id1, _ = await decide("NEED_REPLAN", SNAP)
    _, id2, _ = await decide("NEED_REPLAN", SNAP)
    assert id1 != id2                                   # 61% > 50% → 세션 교체
    assert trace.events(event_type="LeaderCompactEvent")


async def test_no_compaction_when_measurement_is_low(tmp_path):
    orch, trace, fake, cfg = make_env(tmp_path, [GOOD, GOOD, GOOD])
    fake.context = {"used": 100_000, "window": 1_000_000, "pct": 10.0, "source": "sdk"}
    state: dict = {}
    decide = make_llm_decide(orch, cfg, leader_state=state, compact_at=1,
                             compact_ratio=0.5)
    _, id1, _ = await decide("NEED_REPLAN", SNAP)
    _, id2, _ = await decide("NEED_REPLAN", SNAP)
    assert id1 == id2                                   # 실측 10% — 추정 임계는 무시된다
    assert not trace.events(event_type="LeaderCompactEvent")
