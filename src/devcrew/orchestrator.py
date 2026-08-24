"""Orchestrator 원시 연산 — spawn/escalation(§7.6), consume_result 전이 판정(§10),
queue(§9.1). Bounded loop/decision 오케스트레이션 자체는 `WorkflowEngine`(engine.py,
§10) 몫이다.

POC 범위: 상태 전이와 이벤트 기록의 실행 가능성 증명. Slack/Task Service는 미포함.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from .adapters.base import TurnOutcome
from .routing import resolve
from .roles import (STATUS_ENUM, RoleBundle, RoleBundleError, load_bundle,
                    missing_required_keys)
from .schema import AgentInstance, EffortLevel, InstanceStatus, Provider, Role
from .store.registry import SessionRegistry
from .store.trace import TraceStore

_EFFORT_BY_STR = {"low": EffortLevel.LOW, "medium": EffortLevel.MEDIUM,
                  "high": EffortLevel.HIGH, "xhigh": EffortLevel.XHIGH,
                  "max": EffortLevel.MAX}

WORKER_ROLES = {Role.EXPLORER, Role.DEVELOPER, Role.REVIEWER, Role.QA}
# role 번들이 존재해 spawn 시 로드해야 하는 role 전체 (워크플로 worker + 결정/대화 role).
# 여기 빠지면 spawn()이 role_bundle_version을 안 찍고, start_worker()가 재로드한
# 번들과 버전이 달라(None != 실제 버전) RoleBundleError로 죽는다.
BUNDLED_ROLES = WORKER_ROLES | {Role.ORCHESTRATOR, Role.BRAIN,
                                Role.TUTOR, Role.TUTOR_VERIFIER, Role.TUTOR_TA,
                                Role.TUTOR_CODE}


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
        # Load role bundle for WORKER_ROLES + ORCHESTRATOR (decision sessions need
        # their own prompt/output_schema bundle too — Task 5)
        if role in BUNDLED_ROLES:
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

    async def start_worker(self, inst: AgentInstance, initial_message: str, *,
                           mcp_servers: dict | None = None,
                           conversational: bool = False) -> str:
        """Start a worker agent session with its role bundle injected.

        `mcp_servers` is passed through to the adapter's start_session (Task 5/6) —
        used by decision sessions (ORCHESTRATOR) to expose the read-only harness MCP
        tools. Workers spawned by the engine don't pass this, so it defaults to None.

        Loads the bundle for the worker role and passes system_prompt and output_schema
        to the adapter's start_session method.

        Re-loads the bundle here (rather than trusting spawn()'s recorded version) and
        verifies its version matches `inst.role_bundle_version`, raising RoleBundleError
        on a mismatch — guards against role bundle files changing between spawn() and
        start_worker(), or callers constructing an AgentInstance without going through
        spawn() at all (finding #5).

        task_scope is injected into the TRUSTED system prompt (bundle.prompt +
        assignment scope), not left to the caller's initial_message, so a worker cannot
        have its assigned scope silently omitted or overridden by untrusted turn content
        (finding #7, partial — Bash/tool-level scope confinement stays a separate parked
        item).
        """
        bundle = load_bundle(inst.role)
        if bundle.version != inst.role_bundle_version:
            raise RoleBundleError(
                f"role bundle drift for {inst.role.value}: instance recorded version "
                f"{inst.role_bundle_version!r} but current bundle is {bundle.version!r} "
                "(bundle files changed since spawn(), or instance was never spawned)")
        adapter = self.adapters[inst.provider]
        system_prompt = bundle.prompt
        if inst.task_scope:
            system_prompt = f"{bundle.prompt}\n\n## 할당 Scope\n{inst.task_scope}"
        if inst.worktree:
            # 워커가 자기 worktree 밖(부모 repo·다른 worktree)의 코드를 읽고 "내
            # 작업 대상은 저쪽인데 여기에 묶여 있다"고 오판해 BLOCKED로 자폭하는
            # 사례(2026-08-20 SLACK-3) 차단. 격리는 결함이 아니라 설계다.
            system_prompt += (
                f"\n\n## 작업 디렉토리 (정본)\n{inst.worktree}\n"
                "이 디렉토리가 네 과업의 정본이다. 이 repo의 다른 경로(부모 repo, "
                "다른 worktree)에 비슷하거나 더 최신인 코드가 보이더라도 그것은 네 "
                "과업 대상이 아니다. 쓰기가 이 디렉토리로 제한되는 것은 하네스의 "
                "격리 설계이지 구성 결함이 아니므로, 그걸 이유로 작업을 중단하지 "
                "마라. 필요한 파일이 여기 없으면 여기서 만들면 된다.")
        # conversational=True는 대화형 role(BRAIN 인터뷰) 전용: provider 구조화 출력이
        # 매 turn을 JSON으로 강제하면 자연어 인터뷰가 불가능하므로 output_schema 주입만
        # 생략한다. role prompt/tool policy/cwd 강제는 그대로 유지된다. 최종 brief는
        # 별도의 비대화(conversational=False) 세션이 스키마 강제로 산출한다.
        #
        # **이 플래그가 하는 일은 스키마 생략 하나뿐이다** — 세션이 turn을 넘어
        # 유지되는지와는 무관하다(세션 지속은 adapter.start_session의 성질이다).
        # "여러 turn을 이어 쓰니까 conversational이겠지"로 읽고 붙이면 그 세션은
        # structured_output을 영영 못 받는다 (2026-08-24 C1: TUTOR_TA가 그렇게
        # 실패했다 — 매 turn이 스키마 제출인 role은 이 플래그를 쓰면 안 된다).
        session_id = await adapter.start_session(
            inst, initial_message,
            system_prompt=system_prompt,
            output_schema=None if conversational else bundle.schema,
            mcp_servers=mcp_servers)
        inst.session_id = session_id
        self.registry.upsert(inst, provider_ref=None)
        return session_id

    def consume_result(self, inst: AgentInstance, outcome: TurnOutcome, *,
                       as_role: Role | None = None) -> str:
        """Structured worker 결과를 상태 전이 문자열로 소비한다 (spec 결정 4, finding #3/#4).

        `outcome.structured`가 dict가 아니거나 `status`가 roles.STATUS_ENUM 밖이면
        malformed로 간주해 `MalformedResultEvent`를 남기고 "NEED_REPLAN"을 반환한다
        (fail-closed — 신뢰할 수 없는 출력으로 상태를 전이시키지 않는다).

        전이 판정에 쓰는 role은 `as_role or inst.role`이다. 기본은 `inst.role`이지만,
        `outcome`이 `inst`와 다른 role의 결과일 때 호출자가 `as_role=Role.REVIEWER`로
        실제 판정 규칙을 명시해야 한다 — 그렇지 않으면 `inst.role`로 판정해 Reviewer의
        verdict가 완전히 무시되고 `{status: PASS, verdict: NOT_PASS}`가 status만으로
        성공 처리되는 버그가 재발한다(재재리뷰 신규 finding). `WorkflowEngine`(§10)은
        모든 `TurnOutcome`을 그 결과를 생산한 노드의 instance로 전달하므로(예: review
        노드의 결과는 그 노드의 Reviewer instance로) `as_role` 없이도 `inst.role`이
        항상 정확한 판정 role이다.

        판정 role이 REVIEWER인 경우는 예외다: `structured["status"]`는 검토를
        "수행"했는지(PASS=검토를 마쳤다, BLOCKED=검토 불가 등)를 나타낼 뿐 코드에
        대한 판정이 아니다 — 코드 판정은 `structured["verdict"]`(PASS/NOT_PASS)에
        있다(roles/reviewer/prompt.md 보고 규칙). status가 "PASS"(검토를 실제로
        마쳤을 때)에만 verdict를 전이값으로 쓴다. status가 그 외(BLOCKED 등,
        검토가 수행되지 않음)면 verdict를 무시하고 status를 그대로 전파한다 —
        `{status: BLOCKED, verdict: PASS}`처럼 스키마상 유효하지만 검토 불가인
        결과가 verdict만 보고 성공(PASS)으로 진행되는 걸 막는다. status가 PASS인데
        verdict가 PASS/NOT_PASS가 아니면 malformed로 처리한다. 판정 role이 그 외면
        status를 그대로 전이값으로 쓴다.

        정상 경로에서는 `WorkerResultEvent`를 남긴다 (payload: 판정에 쓰인 role,
        status, 그리고 REVIEWER 판정이 실제로 검토를 마친 경우 verdict도 포함).
        `structured` 전문도 payload에 포함한다 (MCP get_worker_result의 데이터 소스;
        §5 constraint상 프롬프트 원문이 아니라 구조화 결과이므로 trace payload에
        남겨도 무방하다).

        finding #2 — provider의 output_schema 강제(1차 방어)를 우회한 malformed
        출력(예: 필수 필드 누락)을 잡는 2차 방어로, `status`만이 아니라 판정
        role의 role bundle 전체 schema를 `missing_required_keys`(경량 required-키
        재귀 검사, jsonschema 의존성 없음)로 확인한다. 누락이 있으면 status가
        유효한 값이어도 malformed로 강등한다.
        """
        role = as_role or inst.role
        structured = outcome.structured
        malformed = (
            not isinstance(structured, dict)
            or structured.get("status") not in STATUS_ENUM
            or missing_required_keys(load_bundle(role).schema, structured))
        if malformed:
            self.trace.append("MalformedResultEvent", task_id=inst.execution_id,
                              execution_id=inst.execution_id, instance_id=inst.instance_id,
                              payload={"role": role.value, "raw": structured})
            return "NEED_REPLAN"

        status = structured["status"]
        if role is Role.REVIEWER and status == "PASS":
            verdict = structured.get("verdict")
            if verdict not in ("PASS", "NOT_PASS"):
                self.trace.append("MalformedResultEvent", task_id=inst.execution_id,
                                  execution_id=inst.execution_id, instance_id=inst.instance_id,
                                  payload={"role": role.value, "raw": structured})
                return "NEED_REPLAN"
            payload = {"role": role.value, "status": status, "verdict": verdict,
                      "structured": structured}
            transition = verdict
        else:
            payload = {"role": role.value, "status": status, "structured": structured}
            transition = status

        self.trace.append("WorkerResultEvent", task_id=inst.execution_id,
                          execution_id=inst.execution_id, instance_id=inst.instance_id,
                          payload=payload)
        return transition
