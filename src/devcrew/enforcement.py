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
}


def claude_options_kwargs(role: Role, *, cwd: str | None) -> dict:
    p = ROLE_POLICY[role]
    return {
        "allowed_tools": list(p.allowed_tools),
        "permission_mode": p.permission_mode,
        "cwd": cwd,
    }


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
