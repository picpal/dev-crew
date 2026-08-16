from devcrew.enforcement import (
    ROLE_POLICY, claude_options_kwargs, codex_session_kwargs, make_can_use_tool,
)
from devcrew.schema import Role
from devcrew.store.trace import TraceStore


def test_role_policy_matches_design_3_4():
    assert "Write" not in ROLE_POLICY[Role.EXPLORER].allowed_tools
    assert "Read" in ROLE_POLICY[Role.EXPLORER].allowed_tools
    # Developer는 Write를 scoped_write_tools로 관리 (경로 제한)
    assert "Write" not in ROLE_POLICY[Role.DEVELOPER].allowed_tools
    assert "Write" in ROLE_POLICY[Role.DEVELOPER].scoped_write_tools
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
    assert kw["approval_mode_name"] == "deny_all"


async def test_can_use_tool_denies_and_logs(tmp_path):
    # SDK 런타임은 {"behavior": ...} dict가 아니라 PermissionResultAllow/Deny
    # 인스턴스를 요구한다 (claude-agent-sdk==0.2.139 조사, Task 7).
    trace = TraceStore(tmp_path / "trace.db")
    cb = make_can_use_tool(Role.EXPLORER, trace, task_id="T-1")
    allow = await cb("Read", {"file_path": "/x"}, None)
    deny = await cb("Write", {"file_path": "/x"}, None)
    bash_deny = await cb("Bash", {"command": "echo hi > /x"}, None)
    assert allow.behavior == "allow"
    assert deny.behavior == "deny"
    assert bash_deny.behavior == "deny"
    evs = trace.events(event_type="PermissionDeniedEvent")
    assert len(evs) == 2
    assert evs[0]["payload"]["tool"] == "Write"
    assert evs[1]["payload"]["tool"] == "Bash"


async def test_scoped_write_confinement(tmp_path):
    """Developer Write/Edit는 workspace_root 내에서만 허용 (#13 결정)."""
    trace = TraceStore(tmp_path / "trace.db")
    workspace = str(tmp_path / "workspace")

    # workspace_root이 설정된 경우
    cb = make_can_use_tool(Role.DEVELOPER, trace, task_id="T-1", workspace_root=workspace)

    # workspace 내의 파일 쓰기는 허용
    allow_inside = await cb("Write", {"file_path": f"{workspace}/x.txt"}, None)
    assert allow_inside.behavior == "allow"

    # workspace 밖의 파일 쓰기는 거부
    deny_outside = await cb("Write", {"file_path": "/etc/passwd"}, None)
    assert deny_outside.behavior == "deny"

    # 거부 이벤트 확인
    evs = trace.events(event_type="PermissionDeniedEvent")
    assert len(evs) == 1
    assert evs[0]["payload"]["tool"] == "Write"
    assert evs[0]["payload"]["reason"] == "path_outside_workspace"
