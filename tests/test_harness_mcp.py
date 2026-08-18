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
    trace.append("DecisionEvent", task_id="E1", execution_id="E1", instance_id="ORC-1",
                payload={"trigger": "NEED_REPLAN",
                        "decision": {"action": "ABORT", "target_node": None, "rationale": "r"}})
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
