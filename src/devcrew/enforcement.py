"""Role capability enforcement (#13) — 선언적 policy → provider 네이티브 변환.

경계는 콜백/샌드박스가, 관측은 메시지 스트림이 담당한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .schema import Role
from .store.trace import TraceStore

_READ_TOOLS = ["Read", "Grep", "Glob"]
_WRITE_TOOLS = ["Write", "Edit"]


@dataclass(frozen=True)
class RolePolicy:
    allowed_tools: list[str] = field(default_factory=list)
    permission_mode: str = "default"
    sandbox: str | None = None          # Codex 전용: "read-only" 등


# DESIGN.md §3.4 표의 코드화
ROLE_POLICY: dict[Role, RolePolicy] = {
    Role.ORCHESTRATOR: RolePolicy(allowed_tools=[]),   # repo tool 전무
    Role.EXPLORER: RolePolicy(allowed_tools=[*_READ_TOOLS, "Bash(git log:*)", "Bash(git diff:*)"]),
    Role.ARCHITECT: RolePolicy(allowed_tools=list(_READ_TOOLS)),
    Role.DEVELOPER: RolePolicy(allowed_tools=[*_READ_TOOLS, *_WRITE_TOOLS, "Bash"],
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
    return {"sandbox_name": (p.sandbox or "workspace-write").replace("-", "_"), "cwd": cwd}


def make_can_use_tool(role: Role, trace: TraceStore, *, task_id: str):
    """Claude SDK can_use_tool 콜백 — allowlist 밖 호출 거부 + 이벤트 적재."""
    allowed = ROLE_POLICY[role].allowed_tools

    def _match(tool_name: str) -> bool:
        for pat in allowed:
            base = pat.split("(")[0]
            if tool_name == base:
                return True
        return False

    async def can_use_tool(tool_name: str, tool_input: dict, context) -> dict:
        if _match(tool_name):
            return {"behavior": "allow", "updatedInput": tool_input}
        trace.append("PermissionDeniedEvent", task_id=task_id,
                     payload={"role": role.value, "tool": tool_name})
        return {"behavior": "deny",
                "message": f"role {role.value} may not use {tool_name}"}

    return can_use_tool
