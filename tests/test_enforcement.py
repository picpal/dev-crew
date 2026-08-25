import pytest

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
    # Orchestrator는 repo tool 전무(#13, 불변 조건 2) — harness MCP read-only tool
    # 3종만 허용된다(Task 6).
    assert ROLE_POLICY[Role.ORCHESTRATOR].allowed_tools == [
        "mcp__harness__get_execution_state", "mcp__harness__get_worker_result",
        "mcp__harness__get_trace_events"]
    assert not ROLE_POLICY[Role.ORCHESTRATOR].scoped_write_tools
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


async def test_scoped_write_without_workspace_root_is_denied(tmp_path):
    """Regression: workspace_root이 None이면 Developer Write는 거부 (fail-closed)."""
    trace = TraceStore(tmp_path / "trace.db")
    # workspace_root 미설정
    cb = make_can_use_tool(Role.DEVELOPER, trace, task_id="T-2", workspace_root=None)

    deny = await cb("Write", {"file_path": "/tmp/x.txt"}, None)
    assert deny.behavior == "deny"

    evs = trace.events(event_type="PermissionDeniedEvent")
    assert len(evs) == 1
    assert evs[0]["payload"]["tool"] == "Write"
    assert evs[0]["payload"]["reason"] == "no_workspace_root"


async def test_sibling_prefix_dir_is_not_allowed(tmp_path):
    """Regression: /tmp/root-evil는 /tmp/root의 sibling이므로 거부."""
    trace = TraceStore(tmp_path / "trace.db")
    workspace = str(tmp_path / "root")

    cb = make_can_use_tool(Role.DEVELOPER, trace, task_id="T-3", workspace_root=workspace)

    # 정확한 workspace 내 파일은 허용
    allow = await cb("Write", {"file_path": f"{workspace}/file.txt"}, None)
    assert allow.behavior == "allow"

    # Sibling-like prefix는 거부 (is_relative_to 시맨틱 보호)
    deny = await cb("Write", {"file_path": f"{workspace}-evil/file.txt"}, None)
    assert deny.behavior == "deny"

    evs = trace.events(event_type="PermissionDeniedEvent")
    assert len(evs) == 1
    assert evs[0]["payload"]["reason"] == "path_outside_workspace"


def test_every_role_has_a_policy():
    """새 Role을 추가하고 정책을 빼면 spawn이 KeyError로 죽는다. 이름을 하나씩
    검사하는 테스트로는 '빠진 role'을 못 잡는다 — enum 전수로 검사한다.
    (2026-08-21: TUTOR·TUTOR_VERIFIER가 이렇게 빠져 첫 회차가 통째로 실패했다)"""
    missing = [r.value for r in Role if r not in ROLE_POLICY]
    assert missing == [], f"ROLE_POLICY에 없는 role: {missing}"


@pytest.mark.parametrize("role", list(Role))
def test_option_builders_work_for_every_role(role):
    """spawn 경로가 실제로 부르는 두 빌더가 모든 role에서 서야 한다."""
    assert claude_options_kwargs(role, cwd="/tmp")["cwd"] == "/tmp"
    assert codex_session_kwargs(role, cwd="/tmp")["approval_mode_name"] == "deny_all"


def test_tutor_roles_are_read_only():
    """출제·검증은 repo를 읽기만 한다. 쓰기나 Bash를 주면 학습 도구가 코드를 만진다."""
    for role in (Role.TUTOR, Role.TUTOR_VERIFIER):
        p = ROLE_POLICY[role]
        assert "Read" in p.allowed_tools and "Grep" in p.allowed_tools
        assert p.scoped_write_tools == []
        assert not any(t.startswith("Bash") for t in p.allowed_tools)
    # 검증자는 Codex 세션이다 — 샌드박스도 읽기 전용으로 못 박는다
    assert ROLE_POLICY[Role.TUTOR_VERIFIER].sandbox == "read-only"


# ── setting_sources: 워커에 사용자·프로젝트 설정을 딸려 보내지 않는다 ──────────
def test_every_role_pins_setting_sources():
    """`setting_sources`를 안 넘기면 SDK가 CLI 기본값에 맡기고, 그건 **전부 로드**다.

    실측(2026-08-25, `claude -p` + 도구 차단): 플래그가 없으면 cwd의 CLAUDE.md가
    컨텍스트에 주입되고, `--setting-sources=`면 주입되지 않는다. 즉 '안 넘기면
    격리'가 아니라 '안 넘기면 전부'다 — 넘기는 것이 좁히는 쪽이다.
    """
    for role in Role:
        kw = claude_options_kwargs(role, cwd="/tmp/wt")
        assert "setting_sources" in kw, f"{role.value}: 기본값에 맡기면 전부 로드된다"
        assert "project" not in kw["setting_sources"], role.value
        assert "local" not in kw["setting_sources"], role.value


def test_only_skill_roles_load_the_user_source():
    """스킬을 선언한 role만 `user`를 연다 — 전역 스킬이 거기 살기 때문이다.

    `[]`로는 SDK가 스킬을 못 찾는다: 자동 보정(`_apply_skills_defaults`)은
    `setting_sources is None`일 때만 발동하므로 명시적으로 열어 줘야 한다.
    """
    for role in Role:
        sources = claude_options_kwargs(role, cwd="/tmp/wt")["setting_sources"]
        assert sources == (["user"] if ROLE_POLICY[role].skills else []), role.value
