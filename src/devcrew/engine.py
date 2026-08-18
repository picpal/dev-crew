"""결정적 워크플로 엔진 (spec 결정 1·6·7).

- 규정된 전이는 즉시 처리, DECISION 트리거에서만 decide_fn 호출
- 모든 TurnOutcome은 그 결과를 생산한 instance로 consume_result에 전달 (attribution)
- decide_fn: async (trigger: str, snapshot: dict) -> (decision: dict,
  producer_instance_id: str | None, usage_tokens: int). decide_fn이 없으면 모든
  결정 지점에서 ({"action": "ASK_USER", ...}, None, 0)로 처리 (LLM 없는 안전 기본값).
  엔진은 decide_fn 호출을 예외/비정형 반환까지 방어한다 (finding #4) — 예외가
  나거나 이 3-tuple 계약을 지키지 않으면 ASK_USER로 강등한다.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from .adapters.base import TurnOutcome
from .config import HarnessConfig
from .schema import AgentInstance, Usage
from .workflow import (ALLOWED_BY_TRIGGER, DEFAULT_TEMPLATE, NodeSpec, Step,
                       WorkflowError, WorkflowTemplate, next_step)

# §7.6 MODEL_ESCALATION 사다리 — 엔진 init에서 cfg.tiers 존재 검증
ESCALATION_LADDER = {"CHEAP": "DEFAULT", "DEFAULT": "HIGH_CAPABILITY",
                     "CODEX_DEFAULT": "CODEX_HIGH_REASONING"}

# finding #1 — REPLAN/RETRY_NODE는 실행 전체에서 각 2회까지만 적용된다 (3회째부터는
# 결정을 신뢰하지 않고 ASK_USER로 강등). LOOP_GUARD_EXCEEDED 트리거가 실행 전체에서
# 2회를 넘게 발생하면 더는 decide_fn을 부르지 않고 즉시 NEEDS_HUMAN으로 종료한다.
MAX_ACTION_APPLICATIONS = 2
MAX_LOOP_GUARD_TRIGGERS = 2

REPORT_MSG = "이제 최종 보고를 스키마대로 제출해."
FOLLOW_UP_MSG = "위 결과를 반영해 수정/재수행 후 스키마대로 최종 보고해."
# ADVANCE로 재진입하는 노드(예: review가 develop 루프백 이후 다시 도달)는 직전
# LOOP/DECIDE 분기가 만든 follow-up 메시지가 없다 — 세션이 이미 있는데 메시지가
# None이면 실 어댑터(Claude SDK/Codex SDK)가 None 프롬프트를 거부해 크래시한다
# (Task 4 R2 근본원인, task-7-report.md). run_node()의 재진입 경로 전체에 이 기본
# 메시지를 적용해 None이 adapter.send로 새 나가는 경로를 원천 차단한다.
REVISIT_MSG = "이전 보고 이후 작업 상태가 변경됐다. 동일 과업을 다시 수행하고 스키마대로 최종 보고해."

_ASK_USER_NO_DECIDE_FN = {"action": "ASK_USER", "target_node": None,
                          "rationale": "no decide_fn configured"}


def _tokens(u: Usage) -> int:
    return (u.input_tokens or 0) + (u.output_tokens or 0)


def _follow_up_msg(structured: dict | None) -> str:
    return f"{FOLLOW_UP_MSG} 이전 결과: {json.dumps(structured, ensure_ascii=False)}"


def _same_finding_signature(node_id: str, structured: dict | None) -> tuple:
    """LOOP 재진입이 "동일 finding"인지 판정하는 서명 (finding #1e).

    `structured["findings"]`가 리스트면 (file, severity, description) 튜플들을
    정렬해 해시한 값을 서명으로 쓴다 — findings의 원소 순서/추가 필드가 달라져도
    같은 이슈 집합이면 같은 서명이 나온다. findings가 없으면 기존처럼
    (node_id, summary)로 폴백한다 (예: DEVELOPER 등 findings 스키마가 없는 role).
    """
    findings = structured.get("findings") if isinstance(structured, dict) else None
    if isinstance(findings, list):
        normalized = tuple(sorted(
            (f.get("file"), f.get("severity"), f.get("description"))
            for f in findings if isinstance(f, dict)))
        digest = hashlib.sha256(repr(normalized).encode()).hexdigest()
        return (node_id, digest)
    summary = structured.get("summary") if isinstance(structured, dict) else None
    return (node_id, summary)


@dataclass
class NodeRuntime:
    spec: NodeSpec
    tier: str
    inst: AgentInstance | None = None
    session_id: str | None = None
    iterations: int = 0
    skipped: bool = False


@dataclass(frozen=True)
class ExecutionResult:
    status: str                  # COMPLETED | NEEDS_HUMAN | ABORTED
    node_history: list[dict]     # [{node_id, transition, iteration}]
    decisions: int
    total_tokens: int


class WorkflowEngine:
    def __init__(self, orch, cfg: HarnessConfig, *, template: WorkflowTemplate = DEFAULT_TEMPLATE,
                 decide_fn=None, clock=time.monotonic):
        # ESCALATION_LADDER가 참조하는 모든 tier가 cfg에 존재하는지 init에서 검증
        # (fail-fast — 런타임 escalation 시점에 KeyError로 조용히 죽는 것을 방지)
        for src, dst in ESCALATION_LADDER.items():
            if src not in cfg.tiers or dst not in cfg.tiers:
                raise WorkflowError(
                    f"ESCALATION_LADDER references unknown tier(s): {src} -> {dst}")
        self.orch = orch
        self.cfg = cfg
        self.template = template
        self.decide_fn = decide_fn
        self.clock = clock

    async def run(self, *, execution_id: str, task: str, worktree: str | None = None) -> ExecutionResult:
        template = self.template
        lp = self.cfg.loop_policy
        nodes = [NodeRuntime(spec=n, tier=self.cfg.role_defaults[n.role].tier)
                 for n in template.nodes]

        started = self.clock()
        total_tokens = 0
        decisions = 0
        node_history: list[dict] = []
        same_finding_sig = None
        same_finding_count = 0
        # finding #1 — 실행 전체 누적 카운터. 어떤 재진입/리셋 경로도 이 값들을
        # 되돌리지 않는다 (per-node `iterations`/`same_finding_*`와 달리).
        node_visits_total = 0
        replan_count = 0
        retry_count = 0
        loop_guard_trigger_count = 0

        def find_node(node_id: str | None) -> NodeRuntime | None:
            if node_id is None:
                return None
            return next((r for r in nodes if r.spec.node_id == node_id), None)

        def make_snapshot(trigger: str, rt: NodeRuntime | None, worker_result: dict | None) -> dict:
            return {
                "trigger": trigger, "execution_id": execution_id, "task": task,
                "current_node": rt.spec.node_id if rt else None,
                "nodes": [{"node_id": r.spec.node_id, "role": r.spec.role.value,
                          "conditional": r.spec.conditional, "skipped": r.skipped,
                          "iterations": r.iterations} for r in nodes],
                "worker_result": worker_result,
                "history": node_history[-10:],
                "allowed_actions": ALLOWED_BY_TRIGGER[trigger],
                "loop_policy": {"max_iterations": lp.max_iterations,
                               "max_duration_minutes": lp.max_duration_minutes,
                               "max_token_budget": lp.max_token_budget,
                               "same_finding_escalation_threshold":
                                   lp.same_finding_escalation_threshold},
            }

        async def call_decide_fn(trigger: str, rt: NodeRuntime | None,
                                 worker_result: dict | None) -> tuple[dict, str | None, int]:
            """decide_fn을 호출해 (raw_decision, producer_instance_id, usage_tokens)를
            반환한다 (finding #5/#6 계약). decide_fn이 없거나, 예외를 내거나, 이
            3-tuple 계약을 지키지 않으면 ASK_USER로 강등한다 (finding #4, fail-closed
            — engine 경계에서도 callback을 신뢰하지 않는다).
            """
            nonlocal decisions
            decisions += 1
            snapshot = make_snapshot(trigger, rt, worker_result)
            if self.decide_fn is None:
                return dict(_ASK_USER_NO_DECIDE_FN), None, 0
            try:
                result = await self.decide_fn(trigger, snapshot)
            except Exception as e:
                return ({"action": "ASK_USER", "target_node": None,
                        "rationale": f"decide_fn raised: {e!r}"}, None, 0)
            if (isinstance(result, tuple) and len(result) == 3
                    and isinstance(result[0], dict) and "action" in result[0]):
                raw, producer_id, usage_tokens = result
                return raw, producer_id, usage_tokens or 0
            return ({"action": "ASK_USER", "target_node": None,
                     "rationale": f"decide_fn returned invalid result: {result!r}"}, None, 0)

        def record_decision(trigger: str, raw: dict, applied: dict,
                            producer_id: str | None, *, degraded: bool) -> None:
            self.orch.trace.append(
                "DecisionEvent", task_id=execution_id, execution_id=execution_id,
                instance_id=producer_id,
                payload={"trigger": trigger, "raw": raw, "applied": applied,
                        "degraded": degraded})

        async def run_node(rt: NodeRuntime, follow_up_msg: str | None) -> TurnOutcome:
            nonlocal node_visits_total, total_tokens
            node_visits_total += 1
            if rt.session_id is None:
                rt.inst = await self.orch.spawn(
                    rt.spec.role, rt.tier, execution_id=execution_id, node_id=rt.spec.node_id,
                    task_scope=task, worktree=worktree)
                rt.session_id = await self.orch.start_worker(
                    rt.inst, rt.spec.message.format(task=task))
                adapter = self.orch.adapters[rt.inst.provider]
                # finding #6 — start_worker의 최초 turn usage는 adapter.start_session
                # 내부로 버려진다 (반환값은 session_id뿐). 첫 방문 직후 그 usage를
                # 별도로 복구해 합산한다.
                initial = await adapter.initial_usage(rt.session_id)
                if initial is not None:
                    total_tokens += _tokens(initial)
                return await adapter.send(rt.session_id, REPORT_MSG)
            # 재진입(세션 기존) — ADVANCE는 follow_up_msg=None을 넘길 수 있으니
            # (LOOP/RETRY_NODE/REPLAN은 항상 non-None 메시지를 만든다) None이면
            # 명시적 재수행 메시지로 대체한다. adapter.send가 None을 받는 경로는 없다.
            adapter = self.orch.adapters[rt.inst.provider]
            message = follow_up_msg if follow_up_msg is not None else REVISIT_MSG
            return await adapter.send(rt.session_id, message)

        # 조건부 노드가 하나라도 있으면 CLASSIFY 결정을 1회만 호출한다 (SKIP_NODE는 target 1개만)
        if any(n.conditional for n in template.nodes):
            raw, producer_id, usage_tokens = await call_decide_fn("CLASSIFY", None, None)
            total_tokens += usage_tokens
            action = raw.get("action")
            if action == "SKIP_NODE":
                target_rt = find_node(raw.get("target_node"))
                if target_rt is None or not target_rt.spec.conditional:
                    # invalid 결정(대상이 조건부 노드가 아님) -> ASK_USER 강등
                    applied = {"action": "ASK_USER", "target_node": None,
                              "rationale": "invalid SKIP_NODE target"}
                    record_decision("CLASSIFY", raw, applied, producer_id, degraded=True)
                    return ExecutionResult("NEEDS_HUMAN", node_history, decisions, total_tokens)
                target_rt.skipped = True
                record_decision("CLASSIFY", raw, raw, producer_id, degraded=False)
            elif action == "PROCEED":
                record_decision("CLASSIFY", raw, raw, producer_id, degraded=False)
            else:
                applied = {"action": "ASK_USER", "target_node": None,
                          "rationale": f"invalid CLASSIFY action {action!r}"}
                record_decision("CLASSIFY", raw, applied, producer_id, degraded=True)
                return ExecutionResult("NEEDS_HUMAN", node_history, decisions, total_tokens)

        async def process(rt: NodeRuntime, outcome: TurnOutcome):
            """rt의 실행 결과를 소비하고 다음 스텝을 계산한다.

            반환: ("goto", idx, follow_up) 또는 ("terminal", status)
            """
            nonlocal total_tokens, same_finding_sig, same_finding_count
            nonlocal node_visits_total, replan_count, retry_count, loop_guard_trigger_count
            total_tokens += _tokens(outcome.usage)
            transition = self.orch.consume_result(rt.inst, outcome)
            step = next_step(rt.spec, transition)
            node_history.append({"node_id": rt.spec.node_id, "transition": transition,
                                 "iteration": rt.iterations})
            self.orch.trace.append(
                "NodeTransitionEvent", task_id=execution_id, execution_id=execution_id,
                instance_id=rt.inst.instance_id,
                payload={"node_id": rt.spec.node_id, "transition": transition,
                        "step_kind": step.kind, "iteration": rt.iterations})

            # finding #1d — 토큰/duration/누적 방문수 guard는 LOOP 분기만이 아니라
            # 모든 노드 완료 시점에 검사한다 (ADVANCE라도 예산 초과분은 통과시키지 않는다).
            elapsed = self.clock() - started
            accumulated_guard_exceeded = (
                node_visits_total > lp.max_iterations * len(nodes)
                or total_tokens > lp.max_token_budget
                or elapsed > lp.max_duration_minutes * 60)

            local_loop_guard_exceeded = False
            loop_target_idx = None
            if step.kind == "LOOP":
                target_idx = template.index(step.target)
                target_rt = nodes[target_idx]
                target_rt.iterations += 1
                sig = _same_finding_signature(rt.spec.node_id, outcome.structured)
                if sig == same_finding_sig:
                    same_finding_count += 1
                else:
                    same_finding_sig, same_finding_count = sig, 1
                local_loop_guard_exceeded = (
                    target_rt.iterations >= lp.max_iterations
                    or same_finding_count >= lp.same_finding_escalation_threshold)
                loop_target_idx = target_idx
                # finding #5 — LOOP 발생마다 생산자(현재 노드) instance의 LoopEvent를
                # 남긴다. guard 초과 여부와 무관하게 매 LOOP 전이마다 기록한다.
                self.orch.trace.append(
                    "LoopEvent", task_id=execution_id, execution_id=execution_id,
                    instance_id=rt.inst.instance_id if rt.inst else None,
                    payload={"iteration": target_rt.iterations, "node_id": rt.spec.node_id,
                            "transition": transition})

            guard_exceeded = accumulated_guard_exceeded or local_loop_guard_exceeded

            if not guard_exceeded:
                if step.kind == "ADVANCE":
                    return ("goto", template.index(rt.spec.node_id) + 1, None)
                if step.kind == "LOOP":
                    return ("goto", loop_target_idx, _follow_up_msg(outcome.structured))

            trigger = "LOOP_GUARD_EXCEEDED" if guard_exceeded else step.trigger

            if trigger == "LOOP_GUARD_EXCEEDED":
                # finding #1c — 이 트리거가 실행 전체에서 2회를 넘게 발생하면 더는
                # decide_fn을 신뢰하지 않는다: 결정 없이 즉시 NEEDS_HUMAN으로 종료한다.
                loop_guard_trigger_count += 1
                if loop_guard_trigger_count > MAX_LOOP_GUARD_TRIGGERS:
                    return ("terminal", "NEEDS_HUMAN")

            raw, producer_id, usage_tokens = await call_decide_fn(trigger, rt, outcome.structured)
            total_tokens += usage_tokens
            allowed = ALLOWED_BY_TRIGGER[trigger]
            action = raw.get("action")
            applied = dict(raw)
            degraded = False

            def degrade(reason: str) -> None:
                nonlocal action, applied, degraded
                action = "ASK_USER"
                applied = {"action": "ASK_USER", "target_node": None, "rationale": reason}
                degraded = True

            if action not in allowed:
                degrade(f"action {action!r} not allowed for trigger {trigger}")

            retry_target = None
            if action == "RETRY_NODE":
                retry_count += 1
                if retry_count > MAX_ACTION_APPLICATIONS:
                    # finding #1b — RETRY_NODE는 실행 전체에서 MAX_ACTION_APPLICATIONS회까지만
                    # 적용된다 (로컬 per-node iteration 리셋으로 이 guard를 우회할 수 없다).
                    degrade(f"RETRY_NODE exceeded max {MAX_ACTION_APPLICATIONS} "
                           "applications this execution")
                else:
                    retry_target = find_node(raw.get("target_node") or rt.spec.node_id)
                    if retry_target is None:
                        # target_node가 template에 없는 노드 -> ASK_USER와 동일하게 강등
                        # (CLASSIFY의 invalid SKIP_NODE target과 같은 fail-safe 패턴)
                        degrade(f"unknown RETRY_NODE target_node {raw.get('target_node')!r}")

            replan_target = None
            if action == "REPLAN":
                replan_count += 1
                if replan_count > MAX_ACTION_APPLICATIONS:
                    degrade(f"REPLAN exceeded max {MAX_ACTION_APPLICATIONS} "
                           "applications this execution")
                else:
                    replan_target = find_node(raw.get("target_node") or "develop")
                    if replan_target is None:
                        degrade(f"unknown REPLAN target_node {raw.get('target_node')!r}")

            if action == "ESCALATE_MODEL" and ESCALATION_LADDER.get(rt.tier) is None:
                degrade(f"no escalation tier beyond {rt.tier}")

            record_decision(trigger, raw, applied, producer_id, degraded=degraded)

            if action == "RETRY_NODE":
                target_idx = template.index(retry_target.spec.node_id)
                retry_target.iterations = 0
                same_finding_sig, same_finding_count = None, 0
                return ("goto", target_idx, _follow_up_msg(outcome.structured))

            if action == "ESCALATE_MODEL":
                next_tier = ESCALATION_LADDER[rt.tier]
                node_visits_total += 1
                rt.inst = await self.orch.spawn(
                    rt.spec.role, next_tier, execution_id=execution_id, node_id=rt.spec.node_id,
                    task_scope=task, worktree=worktree, replaced=rt.inst,
                    escalation_reason=trigger)
                rt.tier = next_tier
                adapter = self.orch.adapters[rt.inst.provider]
                rt.session_id = await self.orch.start_worker(
                    rt.inst, rt.spec.message.format(task=task))
                initial = await adapter.initial_usage(rt.session_id)
                if initial is not None:
                    total_tokens += _tokens(initial)
                rt.iterations = 0
                same_finding_sig, same_finding_count = None, 0
                outcome2 = await adapter.send(rt.session_id, REPORT_MSG)
                return await process(rt, outcome2)

            if action == "REPLAN":
                target_idx = template.index(replan_target.spec.node_id)
                for r in nodes:
                    r.iterations = 0
                same_finding_sig, same_finding_count = None, 0
                return ("goto", target_idx, _follow_up_msg(outcome.structured))

            if action == "ASK_USER":
                return ("terminal", "NEEDS_HUMAN")

            # ABORT (그 외 action은 위에서 이미 ASK_USER로 강등됨)
            return ("terminal", "ABORTED")

        idx = 0
        follow_up: str | None = None
        while True:
            if idx >= len(nodes):
                return ExecutionResult("COMPLETED", node_history, decisions, total_tokens)
            rt = nodes[idx]
            if rt.skipped:
                idx += 1
                continue
            outcome = await run_node(rt, follow_up)
            follow_up = None
            kind, *rest = await process(rt, outcome)
            if kind == "terminal":
                return ExecutionResult(rest[0], node_history, decisions, total_tokens)
            idx, follow_up = rest
