import pytest
from devcrew.adapters.base import FakeAdapter
from devcrew.config import load as load_config
from devcrew.decision import DecisionError, make_llm_decide, validate_decision
from devcrew.orchestrator import Orchestrator
from devcrew.schema import Provider, Role
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
    d = await decide("NEED_REPLAN", snapshot)
    assert d["action"] == "ASK_USER"
    assert fake.turns                              # 재시도로 send 2회 소비됨을 확인
    assert sum(fake.turns.values()) == 2


async def test_llm_decide_happy_path_spawns_orchestrator(tmp_path):
    orch, trace, fake, cfg = make_env(tmp_path, [GOOD])
    decide = make_llm_decide(orch, cfg, mcp_servers={"harness": "sentinel"})
    snapshot = {"execution_id": "E2"}
    d = await decide("NEED_REPLAN", snapshot)
    assert d["action"] == "ABORT"

    routing = trace.events(event_type="ModelRoutingEvent")
    orch_routing = [e for e in routing if e["payload"]["role"] == "ORCHESTRATOR"]
    assert orch_routing and orch_routing[0]["payload"]["selected_tier"] == "HIGH_CAPABILITY"

    instance_events = trace.events(event_type="InstanceEvent")
    assert any(e["payload"]["role"] == "ORCHESTRATOR" for e in instance_events)

    assert fake.last_mcp_servers == {"harness": "sentinel"}
