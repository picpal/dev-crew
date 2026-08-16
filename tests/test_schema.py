import json
from devcrew.schema import (
    AgentInstance, EffortLevel, InstanceStatus, Provider, Role, Usage,
)


def test_effort_level_is_five_levels():
    assert [e.value for e in EffortLevel] == ["LOW", "MEDIUM", "HIGH", "XHIGH", "MAX"]


def test_agent_instance_roundtrip():
    inst = AgentInstance(
        instance_id="DEV-001", role=Role.DEVELOPER, provider=Provider.CLAUDE_CODE,
        adapter="claude-code-adapter", model="claude-sonnet-5",
        effort_level=EffortLevel.HIGH, reasoning_level=None,
        routing_policy_version="v1", routing_reason="default tier",
        session_id=None, execution_id="EXE-1", workflow_id="WF-1",
        node_id="dev-a", task_scope="src/api/**", worktree=None,
    )
    data = json.loads(inst.to_json())
    assert data["role"] == "DEVELOPER" and data["status"] == "CREATED"
    back = AgentInstance.from_json(inst.to_json())
    assert back == inst


def test_usage_union_nullable_and_raw():
    u = Usage(input_tokens=10, output_tokens=5, raw={"provider": "x"})
    assert u.total_cost_usd is None and u.duration_ms is None
    assert u.raw["provider"] == "x"
