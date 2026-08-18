"""결정적 워크플로 엔진 (spec 결정 1·6·7).

- 규정된 전이는 즉시 처리, DECISION 트리거에서만 decide_fn 호출
- 모든 TurnOutcome은 그 결과를 생산한 instance로 consume_result에 전달 (attribution)
- decide_fn: async (trigger: str, snapshot: dict) -> dict  (decision dict 반환).
  None이면 모든 결정 지점에서 {"action": "ASK_USER", ...}로 처리 (LLM 없는 안전 기본값)
"""
from __future__ import annotations

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

REPORT_MSG = "이제 최종 보고를 스키마대로 제출해."
FOLLOW_UP_MSG = "위 결과를 반영해 수정/재수행 후 스키마대로 최종 보고해."


def _tokens(u: Usage) -> int:
    return (u.input_tokens or 0) + (u.output_tokens or 0)


def _follow_up_msg(structured: dict | None) -> str:
    return f"{FOLLOW_UP_MSG} 이전 결과: {json.dumps(structured, ensure_ascii=False)}"


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

        async def decide(trigger: str, rt: NodeRuntime | None, worker_result: dict | None) -> dict:
            nonlocal decisions
            decisions += 1
            snapshot = make_snapshot(trigger, rt, worker_result)
            if self.decide_fn is None:
                d = {"action": "ASK_USER", "target_node": None,
                    "rationale": "no decide_fn configured"}
            else:
                d = await self.decide_fn(trigger, snapshot)
            self.orch.trace.append(
                "DecisionEvent", task_id=execution_id, execution_id=execution_id,
                instance_id=rt.inst.instance_id if rt and rt.inst else None,
                payload={"trigger": trigger, "decision": d})
            return d

        async def run_node(rt: NodeRuntime, follow_up_msg: str | None) -> TurnOutcome:
            if rt.session_id is None:
                rt.inst = await self.orch.spawn(
                    rt.spec.role, rt.tier, execution_id=execution_id, node_id=rt.spec.node_id,
                    task_scope=task, worktree=worktree)
                rt.session_id = await self.orch.start_worker(
                    rt.inst, rt.spec.message.format(task=task))
                adapter = self.orch.adapters[rt.inst.provider]
                return await adapter.send(rt.session_id, REPORT_MSG)
            adapter = self.orch.adapters[rt.inst.provider]
            return await adapter.send(rt.session_id, follow_up_msg)

        # 조건부 노드가 하나라도 있으면 CLASSIFY 결정을 1회만 호출한다 (SKIP_NODE는 target 1개만)
        if any(n.conditional for n in template.nodes):
            d = await decide("CLASSIFY", None, None)
            action = d.get("action")
            if action == "SKIP_NODE":
                target_rt = find_node(d.get("target_node"))
                if target_rt is None or not target_rt.spec.conditional:
                    # invalid 결정(대상이 조건부 노드가 아님) -> ASK_USER 강등
                    return ExecutionResult("NEEDS_HUMAN", node_history, decisions, total_tokens)
                target_rt.skipped = True
            elif action != "PROCEED":
                return ExecutionResult("NEEDS_HUMAN", node_history, decisions, total_tokens)

        async def process(rt: NodeRuntime, outcome: TurnOutcome):
            """rt의 실행 결과를 소비하고 다음 스텝을 계산한다.

            반환: ("goto", idx, follow_up) 또는 ("terminal", status)
            """
            nonlocal total_tokens, same_finding_sig, same_finding_count
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

            if step.kind == "ADVANCE":
                return ("goto", template.index(rt.spec.node_id) + 1, None)

            if step.kind == "LOOP":
                target_idx = template.index(step.target)
                target_rt = nodes[target_idx]
                target_rt.iterations += 1
                sig = (rt.spec.node_id, (outcome.structured or {}).get("summary"))
                if sig == same_finding_sig:
                    same_finding_count += 1
                else:
                    same_finding_sig, same_finding_count = sig, 1
                elapsed = self.clock() - started
                guard_exceeded = (
                    target_rt.iterations >= lp.max_iterations
                    or same_finding_count >= lp.same_finding_escalation_threshold
                    or total_tokens > lp.max_token_budget
                    or elapsed > lp.max_duration_minutes * 60)
                if not guard_exceeded:
                    return ("goto", target_idx, _follow_up_msg(outcome.structured))
                trigger = "LOOP_GUARD_EXCEEDED"
            else:
                trigger = step.trigger

            d = await decide(trigger, rt, outcome.structured)
            allowed = ALLOWED_BY_TRIGGER[trigger]
            action = d.get("action")
            if action not in allowed:
                action = "ASK_USER"      # 트리거에 허용되지 않은 action -> 안전 기본값 강등

            if action == "RETRY_NODE":
                target_idx = template.index(d.get("target_node") or rt.spec.node_id)
                target_rt = nodes[target_idx]
                target_rt.iterations = 0
                same_finding_sig, same_finding_count = None, 0
                return ("goto", target_idx, _follow_up_msg(outcome.structured))

            if action == "ESCALATE_MODEL":
                next_tier = ESCALATION_LADDER.get(rt.tier)
                if next_tier is None:
                    return ("terminal", "NEEDS_HUMAN")
                rt.inst = await self.orch.spawn(
                    rt.spec.role, next_tier, execution_id=execution_id, node_id=rt.spec.node_id,
                    task_scope=task, worktree=worktree, replaced=rt.inst,
                    escalation_reason=trigger)
                rt.tier = next_tier
                rt.session_id = await self.orch.start_worker(
                    rt.inst, rt.spec.message.format(task=task))
                rt.iterations = 0
                same_finding_sig, same_finding_count = None, 0
                adapter = self.orch.adapters[rt.inst.provider]
                outcome2 = await adapter.send(rt.session_id, REPORT_MSG)
                return await process(rt, outcome2)

            if action == "REPLAN":
                target_idx = template.index(d.get("target_node") or "develop")
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
