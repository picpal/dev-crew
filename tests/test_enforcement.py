from devcrew.enforcement import (
    ROLE_POLICY, claude_options_kwargs, codex_session_kwargs, make_can_use_tool,
)
from devcrew.schema import Role
from devcrew.store.trace import TraceStore


def test_role_policy_matches_design_3_4():
    assert "Write" not in ROLE_POLICY[Role.EXPLORER].allowed_tools
    assert "Read" in ROLE_POLICY[Role.EXPLORER].allowed_tools
    assert "Write" in ROLE_POLICY[Role.DEVELOPER].allowed_tools
    # Orchestrator는 repo tool 전무 (#13, 불변 조건 2)
    assert ROLE_POLICY[Role.ORCHESTRATOR].allowed_tools == []
    assert ROLE_POLICY[Role.REVIEWER].sandbox == "read-only"


def test_claude_options_kwargs():
    kw = claude_options_kwargs(Role.EXPLORER, cwd="/tmp/wt")
    assert kw["allowed_tools"] == ROLE_POLICY[Role.EXPLORER].allowed_tools
    assert kw["cwd"] == "/tmp/wt"
    assert "can_use_tool" not in kw          # 콜백은 어댑터가 별도 주입


def test_codex_session_kwargs_reviewer_read_only():
    kw = codex_session_kwargs(Role.REVIEWER, cwd="/tmp/wt")
    assert kw["sandbox_name"] == "read_only"


async def test_can_use_tool_denies_and_logs(tmp_path):
    trace = TraceStore(tmp_path / "trace.db")
    cb = make_can_use_tool(Role.EXPLORER, trace, task_id="T-1")
    allow = await cb("Read", {"file_path": "/x"}, None)
    deny = await cb("Write", {"file_path": "/x"}, None)
    assert allow["behavior"] == "allow"
    assert deny["behavior"] == "deny"
    evs = trace.events(event_type="PermissionDeniedEvent")
    assert len(evs) == 1 and evs[0]["payload"]["tool"] == "Write"
