import sqlite3

import pytest
from devcrew.schema import AgentInstance, EffortLevel, InstanceStatus, Provider, Role
from devcrew.store.trace import TraceStore


def make_inst(iid="DEV-001", status=InstanceStatus.CREATED):
    return AgentInstance(
        instance_id=iid, role=Role.DEVELOPER, provider=Provider.CLAUDE_CODE,
        adapter="claude-code-adapter", model="claude-sonnet-5",
        effort_level=EffortLevel.HIGH, reasoning_level=None,
        routing_policy_version="v1", routing_reason="t",
        session_id=None, execution_id="EXE-1", workflow_id="WF-1",
        node_id="n1", task_scope="*", worktree=None, status=status,
    )


def test_append_and_query(tmp_path):
    ts = TraceStore(tmp_path / "trace.db")
    eid = ts.append("ModelRoutingEvent", task_id="T-1", payload={"tier": "CHEAP"})
    assert eid == 1
    evs = ts.events(event_type="ModelRoutingEvent")
    assert evs[0]["payload"]["tier"] == "CHEAP"
    assert evs[0]["task_id"] == "T-1"


def test_events_are_append_only(tmp_path):
    ts = TraceStore(tmp_path / "trace.db")
    ts.append("LoopEvent", task_id="T-1", payload={})
    con = sqlite3.connect(ts.path)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE events SET event_type='x' WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM events WHERE id=1")


def test_projection_rebuild_from_events(tmp_path):
    ts = TraceStore(tmp_path / "trace.db")
    inst = make_inst()
    ts.upsert_instance(inst)                       # projection + InstanceEvent 기록
    inst.status = InstanceStatus.DONE
    ts.upsert_instance(inst)
    # projection을 지워도 events에서 재구축 가능해야 한다 (§13.1)
    sqlite3.connect(ts.path).execute("DELETE FROM agent_instances").connection.commit()
    n = ts.rebuild_instances()
    assert n == 1
    back = ts.get_instance("DEV-001")
    assert back.status == InstanceStatus.DONE
