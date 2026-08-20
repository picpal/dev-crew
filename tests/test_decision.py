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

GOOD = {"status": "PASS", "summary": "s",
        "decision": {"action": "ABORT", "target_node": None, "rationale": "r"}}


def test_validate_decision_ok():
    d = validate_decision(GOOD, "NEED_REPLAN", DEFAULT_TEMPLATE)
    assert d["action"] == "ABORT"


@pytest.mark.parametrize("bad", [
    None,                                                        # malformed
    {"status": "PASS", "summary": "s"},                          # decision 없음
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
    invalid = {"status": "PASS", "summary": "s",
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
