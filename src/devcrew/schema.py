"""공통 스키마 — DESIGN.md §6.1 AgentInstance, §13 Usage 합집합."""
from __future__ import annotations

import enum
import json
import time
import dataclasses
from dataclasses import dataclass, field


class EffortLevel(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    XHIGH = "XHIGH"
    MAX = "MAX"


class Provider(str, enum.Enum):
    CLAUDE_CODE = "CLAUDE_CODE"
    CODEX = "CODEX"


class Role(str, enum.Enum):
    ORCHESTRATOR = "ORCHESTRATOR"
    EXPLORER = "EXPLORER"
    ARCHITECT = "ARCHITECT"
    DEVELOPER = "DEVELOPER"
    SECURITY = "SECURITY"
    REVIEWER = "REVIEWER"
    QA = "QA"
    BRAIN = "BRAIN"           # 대화형 요구사항 인터뷰 (그릴링) — 워크플로 노드 아님


class InstanceStatus(str, enum.Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    ARCHIVED = "ARCHIVED"
    FAILED_RECOVERY = "FAILED_RECOVERY"


@dataclass(frozen=True)
class Usage:
    """provider 합집합 usage. 전 필드 nullable, raw payload 보존 (#11)."""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None   # Claude
    cache_read_input_tokens: int | None = None       # Claude
    cached_input_tokens: int | None = None           # Codex
    reasoning_output_tokens: int | None = None       # Codex
    total_cost_usd: float | None = None              # Claude (client-side 추정치)
    duration_ms: int | None = None                   # Codex
    raw: dict = field(default_factory=dict)


@dataclass
class AgentInstance:
    """DESIGN.md §6.1 스키마의 Python 표현."""
    instance_id: str
    role: Role
    provider: Provider
    adapter: str
    model: str
    effort_level: EffortLevel
    reasoning_level: EffortLevel | None
    routing_policy_version: str
    routing_reason: str
    session_id: str | None
    execution_id: str
    workflow_id: str
    node_id: str
    task_scope: str
    worktree: str | None
    parent_instance_id: str | None = None
    replaced_instance_id: str | None = None
    escalation_chain_id: str | None = None
    role_bundle_version: str | None = None
    skills: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    status: InstanceStatus = InstanceStatus.CREATED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str) -> "AgentInstance":
        d = json.loads(s)
        d["role"] = Role(d["role"])
        d["provider"] = Provider(d["provider"])
        d["effort_level"] = EffortLevel(d["effort_level"])
        if d.get("reasoning_level"):
            d["reasoning_level"] = EffortLevel(d["reasoning_level"])
        d["status"] = InstanceStatus(d["status"])
        return cls(**d)
