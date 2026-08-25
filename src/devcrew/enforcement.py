"""Role capability enforcement (#13) — 선언적 policy → provider 네이티브 변환.

경계는 콜백/샌드박스가, 관측은 메시지 스트림이 담당한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from .schema import Role
from .store.trace import TraceStore

_READ_TOOLS = ["Read", "Grep", "Glob"]
_WRITE_TOOLS = ["Write", "Edit"]


@dataclass(frozen=True)
class RolePolicy:
    allowed_tools: list[str] = field(default_factory=list)
    scoped_write_tools: list[str] = field(default_factory=list)  # 경로 제한이 필요한 쓰기 도구
    permission_mode: str = "default"
    sandbox: str | None = None          # Codex 전용: "read-only" 등
    # 이 role이 쓸 수 있는 Claude Code 스킬. **이름을 명시한 것만** 열린다 —
    # `"all"`은 전역 스킬 전체를 끌어와 컨텍스트를 낭비하고 의도치 않은 능력을 준다.
    skills: list[str] = field(default_factory=list)


# DESIGN.md §3.4 표의 코드화
ROLE_POLICY: dict[Role, RolePolicy] = {
    # repo tool 전무 — harness MCP(read-only)로만 상태를 조회한다 (Task 6, harness_mcp.py)
    Role.ORCHESTRATOR: RolePolicy(allowed_tools=[
        "mcp__harness__get_execution_state", "mcp__harness__get_worker_result",
        "mcp__harness__get_trace_events"]),
    Role.EXPLORER: RolePolicy(allowed_tools=[*_READ_TOOLS, "Bash(git log:*)", "Bash(git diff:*)"]),
    Role.ARCHITECT: RolePolicy(allowed_tools=list(_READ_TOOLS)),
    Role.DEVELOPER: RolePolicy(allowed_tools=[*_READ_TOOLS, "Bash"],
                               scoped_write_tools=list(_WRITE_TOOLS),
                               permission_mode="acceptEdits"),
    Role.SECURITY: RolePolicy(allowed_tools=[*_READ_TOOLS, "Bash(git log:*)"]),
    Role.REVIEWER: RolePolicy(allowed_tools=list(_READ_TOOLS), sandbox="read-only"),
    Role.QA: RolePolicy(allowed_tools=[*_READ_TOOLS, "Bash"]),
    Role.BRAIN: RolePolicy(allowed_tools=list(_READ_TOOLS)),  # 인터뷰 근거용 읽기 전용
    # 학습 Agent (#19) — 출제·검증 모두 repo를 **읽기만** 한다. 학습 도구가 코드를
    # 만질 이유가 없고, Bash도 주지 않는다 (근거는 파일 읽기로 충분하다).
    Role.TUTOR: RolePolicy(allowed_tools=list(_READ_TOOLS)),
    Role.TUTOR_VERIFIER: RolePolicy(allowed_tools=list(_READ_TOOLS), sandbox="read-only"),
    # 후속 질문 답변 — 저장된 해설에 갇히지 않고 repo를 직접 읽는다. 읽기만 한다.
    Role.TUTOR_TA: RolePolicy(allowed_tools=list(_READ_TOOLS)),
    # 학습 도구가 코드를 만질 이유가 없다 — TUTOR_TA와 같은 격리.
    Role.TUTOR_CODE: RolePolicy(allowed_tools=list(_READ_TOOLS)),
    # 그리기는 파일을 만들고 vision의 검증 스크립트를 돌려야 해서 쓰기·Bash가 필요하다.
    # 다른 tutor role과 달리 **cwd가 사용자 repo가 아니다** — `tutor_vis`가 매번
    # 임시 디렉토리를 만들어 넘긴다. 그래서 이 권한이 repo에 닿지 않는다.
    Role.TUTOR_VIS: RolePolicy(
        allowed_tools=[*_READ_TOOLS, *_WRITE_TOOLS, "Bash"],
        permission_mode="acceptEdits", skills=["vision"]),
}


def claude_options_kwargs(role: Role, *, cwd: str | None) -> dict:
    p = ROLE_POLICY[role]
    kw = {
        "allowed_tools": list(p.allowed_tools),
        "permission_mode": p.permission_mode,
        "cwd": cwd,
    }
    # 선언한 role에만 붙인다. `setting_sources`는 넘기지 않는다 — 없이도 Skill 호출이
    # 되는 것을 실측했고(2026-08-25), 넣으면 사용자 전역 설정이 워커에 통째로 딸려 온다.
    if p.skills:
        kw["skills"] = list(p.skills)
    return kw


def codex_session_kwargs(role: Role, *, cwd: str | None) -> dict:
    p = ROLE_POLICY[role]
    # openai_codex.Sandbox enum 멤버 이름으로 전달; 어댑터가 enum으로 변환
    # Enforcement: approval_mode pinned to deny_all for all roles (harness owns enforcement)
    return {"sandbox_name": (p.sandbox or "workspace-write").replace("-", "_"),
            "approval_mode_name": "deny_all", "cwd": cwd}


def make_can_use_tool(role: Role, trace: TraceStore, *, task_id: str, workspace_root: str | None = None):
    """Claude SDK can_use_tool 콜백 — allowlist 밖 호출 거부 + 이벤트 적재.

    SDK 런타임(claude-agent-sdk==0.2.139)은 {"behavior": ...} dict가 아니라
    PermissionResultAllow/PermissionResultDeny 인스턴스를 요구한다 — dict를
    돌려주면 _internal/query.py의 isinstance 분기에서 TypeError가 난다
    (Task 7 조사, task-7-report.md 참조).

    workspace_root이 설정된 경우 scoped_write_tools는 해당 경로로 제한된다.
    Path confinement는 write callback으로만 구현 가능 (Bash는 PreToolUse hook 필요).
    """
    from pathlib import Path

    policy = ROLE_POLICY[role]
    allowed = policy.allowed_tools
    scoped_tools = policy.scoped_write_tools
    workspace_resolved = Path(workspace_root).resolve() if workspace_root else None

    def _match(tool_name: str) -> bool:
        for pat in allowed:
            # Only exact matches (entries without parentheses) are approved here.
            # Pattern entries like "Bash(git log:*)" are evaluated by the SDK natively.
            if pat == tool_name:
                return True
        return False

    def _is_scoped_tool(tool_name: str) -> bool:
        return tool_name in scoped_tools

    async def can_use_tool(tool_name: str, tool_input: dict, context) -> PermissionResultAllow | PermissionResultDeny:
        # Check allowlisted tools
        if _match(tool_name):
            return PermissionResultAllow(updated_input=tool_input)

        # Check scoped write tools (Write, Edit) with path confinement
        if _is_scoped_tool(tool_name):
            if workspace_resolved is not None:
                file_path = tool_input.get("file_path", "")
                if file_path:
                    try:
                        target = Path(file_path).resolve()
                        # Check if target is within workspace
                        if target.is_relative_to(workspace_resolved):
                            return PermissionResultAllow(updated_input=tool_input)
                    except (ValueError, OSError, RuntimeError):
                        # is_relative_to or resolve failed (symlink loops, permission errors, etc.)
                        pass
                trace.append("PermissionDeniedEvent", task_id=task_id,
                             payload={"role": role.value, "tool": tool_name,
                                     "file_path": file_path, "reason": "path_outside_workspace"})
                return PermissionResultDeny(message=f"role {role.value} may not write outside workspace {workspace_resolved}")
            else:
                # Fail-closed: no workspace_root set, but role requires scoped tools → deny
                trace.append("PermissionDeniedEvent", task_id=task_id,
                             payload={"role": role.value, "tool": tool_name,
                                     "reason": "no_workspace_root"})
                return PermissionResultDeny(message=f"role {role.value} requires workspace_root for {tool_name}")

        # Not in any allowlist
        trace.append("PermissionDeniedEvent", task_id=task_id,
                     payload={"role": role.value, "tool": tool_name})
        return PermissionResultDeny(message=f"role {role.value} may not use {tool_name}")

    return can_use_tool
