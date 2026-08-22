import json

import pytest
from devcrew.harness_mcp import _handlers, build_harness_mcp
from devcrew.store.trace import TraceStore


@pytest.fixture
def trace_with_events(tmp_path):
    trace = TraceStore(tmp_path / "trace.db")
    trace.append("NodeTransitionEvent", task_id="E1", execution_id="E1", instance_id="DEV-1",
                payload={"node_id": "develop", "transition": "PASS", "step_kind": "ADVANCE",
                        "iteration": 0})
    trace.append("WorkerResultEvent", task_id="E1", execution_id="E1", instance_id="DEV-1",
                payload={"role": "DEVELOPER", "status": "PASS",
                        "structured": {"status": "PASS", "summary": "done"}})
    # finding #5 — DecisionEvent.payload는 raw(검증 전)/applied(검증·강등 후 실제
    # 적용된 결정)/degraded를 담는다.
    trace.append("DecisionEvent", task_id="E1", execution_id="E1", instance_id="ORC-1",
                payload={"trigger": "NEED_REPLAN",
                        "raw": {"action": "ABORT", "target_node": None, "rationale": "r"},
                        "applied": {"action": "ABORT", "target_node": None, "rationale": "r"},
                        "degraded": False})
    trace.append("WorkerResultEvent", task_id="E1", execution_id="E1", instance_id="REV-1",
                payload={"role": "REVIEWER", "status": "PASS", "verdict": "NOT_PASS",
                        "structured": {"status": "PASS", "summary": "issues",
                                      "verdict": "NOT_PASS"}})
    return trace


async def test_get_trace_events_filters(trace_with_events):
    h = _handlers(trace_with_events)
    out = await h["get_trace_events"]({"execution_id": "E1",
                                       "event_type": "WorkerResultEvent", "limit": 5})
    data = json.loads(out["content"][0]["text"])
    assert all(e["event_type"] == "WorkerResultEvent" for e in data)
    assert len(data) == 2


async def test_get_worker_result_returns_structured(trace_with_events):
    h = _handlers(trace_with_events)
    out = await h["get_worker_result"]({"execution_id": "E1", "instance_id": "REV-1"})
    data = json.loads(out["content"][0]["text"])
    assert data == {"status": "PASS", "summary": "issues", "verdict": "NOT_PASS"}


async def test_get_worker_result_unknown_instance(trace_with_events):
    h = _handlers(trace_with_events)
    out = await h["get_worker_result"]({"execution_id": "E1", "instance_id": "NOPE"})
    assert "not found" in out["content"][0]["text"]


async def test_get_worker_result_requires_instance_id(trace_with_events):
    """finding #7 회귀: instance_id 없이 execution_id만 주면 더 이상 execution의
    "가장 최근" WorkerResultEvent로 조용히 폴백하지 않는다 — 명시적 에러로 거부한다.
    fixture의 최근 WorkerResultEvent는 REV-1(verdict NOT_PASS)이므로, 과거처럼
    폴백했다면 이 호출이 그 결과를 반환해 호출자가 의도한 DEV-1 대신 다른 worker의
    결과로 결정 근거가 조용히 바뀌었을 것이다."""
    h = _handlers(trace_with_events)
    out = await h["get_worker_result"]({"execution_id": "E1"})
    assert "instance_id is required" in out["content"][0]["text"]


async def test_get_execution_state_unknown_execution(trace_with_events):
    h = _handlers(trace_with_events)
    out = await h["get_execution_state"]({"execution_id": "NOPE"})
    assert "not found" in out["content"][0]["text"]


async def test_get_execution_state_synthesizes_nodes_and_decisions(trace_with_events):
    h = _handlers(trace_with_events)
    out = await h["get_execution_state"]({"execution_id": "E1"})
    data = json.loads(out["content"][0]["text"])
    assert data["nodes"][0]["node_id"] == "develop"
    assert data["decisions"][0]["trigger"] == "NEED_REPLAN"
    assert data["decisions"][0]["applied"]["action"] == "ABORT"
    assert data["decisions"][0]["degraded"] is False
    assert data["last_event_at"] is not None


async def test_handlers_never_call_trace_append(trace_with_events, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("MCP handlers must not call TraceStore.append (read-only)")
    monkeypatch.setattr(trace_with_events, "append", boom)
    h = _handlers(trace_with_events)
    await h["get_trace_events"]({"execution_id": "E1"})
    await h["get_worker_result"]({"execution_id": "E1", "instance_id": "DEV-1"})
    await h["get_execution_state"]({"execution_id": "E1"})


def test_build_harness_mcp_server_config(trace_with_events):
    server = build_harness_mcp(trace_with_events)
    assert server["type"] == "sdk"
    assert server["name"] == "harness"


def test_orchestrator_policy_is_mcp_only():
    from devcrew.enforcement import ROLE_POLICY
    from devcrew.schema import Role
    assert ROLE_POLICY[Role.ORCHESTRATOR].allowed_tools == [
        "mcp__harness__get_execution_state", "mcp__harness__get_worker_result",
        "mcp__harness__get_trace_events"]
    assert not ROLE_POLICY[Role.ORCHESTRATOR].scoped_write_tools
