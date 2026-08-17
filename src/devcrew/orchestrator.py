"""Orchestrator 원시 연산 — spawn/escalation(§7.6), bounded loop(§10), queue(§9.1).

POC 범위: 상태 전이와 이벤트 기록의 실행 가능성 증명. Slack/Task Service는 미포함.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from .routing import resolve
from .roles import RoleBundle, RoleBundleError, load_bundle
from .schema import AgentInstance, EffortLevel, InstanceStatus, Provider, Role
from .store.registry import SessionRegistry
from .store.trace import TraceStore

_EFFORT_BY_STR = {"low": EffortLevel.LOW, "medium": EffortLevel.MEDIUM,
                  "high": EffortLevel.HIGH, "xhigh": EffortLevel.XHIGH,
                  "max": EffortLevel.MAX}

WORKER_ROLES = {Role.EXPLORER, Role.DEVELOPER, Role.REVIEWER, Role.QA}


@dataclass(frozen=True)
class LoopResult:
    passed: bool
    iterations: int
    escalated: bool


class ReviewQueue:
    """queue-driven scaling 신호 (§9.1). POC: 길이 기반 필요 인원만 계산."""

    def __init__(self, max_reviewers: int = 3):
        self.max_reviewers = max_reviewers
        self.items: list[str] = []

    def submit(self, item: str) -> None:
        self.items.append(item)

    def scale_signal(self) -> int:
        # 대기 3건당 reviewer 1명 추가, min 1 / max 3
        return max(1, min(self.max_reviewers, 1 + (len(self.items) - 1) // 3))


class Orchestrator:
    def __init__(self, trace: TraceStore, registry: SessionRegistry,
                 adapters: dict[Provider, object]):
        self.trace = trace
        self.registry = registry
        self.adapters = adapters

    async def spawn(self, role: Role, tier_name: str, *, execution_id: str,
                    node_id: str, task_scope: str, worktree: str | None = None,
                    effort: EffortLevel | None = None,
                    replaced: AgentInstance | None = None,
                    escalation_reason: str | None = None) -> AgentInstance:
        r = resolve(tier_name, effort)
        inst = AgentInstance(
            instance_id=f"{role.value[:3]}-{uuid.uuid4().hex[:8]}",
            role=role, provider=r.provider,
            adapter=f"{r.provider.value.lower()}-adapter",
            model=r.model, effort_level=_EFFORT_BY_STR[r.effort],
            reasoning_level=None, routing_policy_version="v1",
            routing_reason=escalation_reason or f"tier {tier_name}",
            session_id=None, execution_id=execution_id, workflow_id=execution_id,
            node_id=node_id, task_scope=task_scope, worktree=worktree,
            replaced_instance_id=replaced.instance_id if replaced else None,
            escalation_chain_id=(replaced.escalation_chain_id or replaced.instance_id)
            if replaced else None,
        )
        # Load role bundle for WORKER_ROLES
        if role in WORKER_ROLES:
            bundle = load_bundle(role)
            inst.role_bundle_version = bundle.version

        self.trace.append("ModelRoutingEvent", task_id=execution_id,
                          execution_id=execution_id, instance_id=inst.instance_id,
                          payload={"role": role.value, "selected_tier": tier_name,
                                   "model": r.model, "effort": r.effort,
                                   "routing_reason": inst.routing_reason})
        if replaced is not None:
            handoff = self.registry.handoff(replaced.instance_id)
            handoff["reason"] = escalation_reason
            self.trace.append("ModelEscalationEvent", task_id=execution_id,
                              execution_id=execution_id, instance_id=inst.instance_id,
                              payload={"from_instance": replaced.instance_id,
                                       "to_instance": inst.instance_id,
                                       "reason": escalation_reason, "handoff": handoff})
            replaced.status = InstanceStatus.FAILED
            self.trace.upsert_instance(replaced)      # 종료 상태 보존
            self.registry.finish(replaced.instance_id)
        self.trace.upsert_instance(inst)
        self.registry.upsert(inst, provider_ref=None)
        return inst

    async def start_worker(self, inst: AgentInstance, initial_message: str) -> str:
        """Start a worker agent session with its role bundle injected.

        Loads the bundle for the worker role and passes system_prompt and output_schema
        to the adapter's start_session method.
        """
        bundle = load_bundle(inst.role)
        adapter = self.adapters[inst.provider]
        session_id = await adapter.start_session(
            inst, initial_message,
            system_prompt=bundle.prompt, output_schema=bundle.schema)
        inst.session_id = session_id
        self.registry.upsert(inst, provider_ref=None)
        return session_id

    async def run_review_loop(self, dev_inst: AgentInstance, review_fn, fix_fn,
                              *, max_iterations: int = 5,
                              same_finding_threshold: int = 3) -> LoopResult:
        last_finding, same_count = None, 0
        for i in range(1, max_iterations + 1):
            verdict = await review_fn(dev_inst)
            self.trace.append("LoopEvent", task_id=dev_inst.execution_id,
                              execution_id=dev_inst.execution_id,
                              instance_id=dev_inst.instance_id,
                              payload={"iteration": i, "verdict": verdict})
            if verdict.startswith("PASS"):
                return LoopResult(passed=True, iterations=i, escalated=False)
            same_count = same_count + 1 if verdict == last_finding else 1
            last_finding = verdict
            if same_count >= same_finding_threshold:
                return LoopResult(passed=False, iterations=i, escalated=True)
            await fix_fn(dev_inst)
        return LoopResult(passed=False, iterations=max_iterations, escalated=True)
