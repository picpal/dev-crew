"""LLM 결정 지점 (spec 결정 3·4) — fresh ORCHESTRATOR 세션 + 구조화 출력 = 결정."""
from __future__ import annotations

import json

from .config import HarnessConfig
from .orchestrator import Orchestrator
from .roles import load_bundle, missing_required_keys
from .schema import Role, Usage
from .workflow import ALLOWED_BY_TRIGGER, DEFAULT_TEMPLATE, WorkflowError, WorkflowTemplate


class DecisionError(Exception):
    pass


def _tokens(u: Usage) -> int:
    return (u.input_tokens or 0) + (u.output_tokens or 0)


def validate_decision(structured, trigger: str, template: WorkflowTemplate) -> dict:
    """구조화 출력 → 검증된 decision dict. 허용 밖·malformed는 DecisionError (fail-closed).

    finding #3 — status/decision object 존재만이 아니라 ORCHESTRATOR role bundle
    schema 전체(summary, decision.target_node, decision.rationale 존재 등)를
    `missing_required_keys`(경량 required-키 재귀 검사)로 검증한다.
    """
    if not isinstance(structured, dict):
        raise DecisionError(f"malformed decision output: {structured!r}")
    missing = missing_required_keys(load_bundle(Role.ORCHESTRATOR).schema, structured)
    if missing:
        raise DecisionError(f"missing required keys in decision output: {missing}")
    if structured.get("status") != "PASS":
        raise DecisionError(f"decision not performed: status={structured.get('status')!r}")
    d = structured.get("decision")
    if not isinstance(d, dict):
        raise DecisionError("missing decision object")
    action, target = d.get("action"), d.get("target_node")
    if action not in ALLOWED_BY_TRIGGER[trigger]:
        raise DecisionError(f"action {action!r} not allowed for trigger {trigger}")
    if action in ("RETRY_NODE", "SKIP_NODE", "REPLAN") and target is not None:
        try:
            template.node(target)   # unknown node → WorkflowError; DecisionError로 감싼다
        except WorkflowError as e:
            raise DecisionError(f"unknown target_node {target!r}: {e}") from e
    return {"action": action, "target_node": target, "rationale": d.get("rationale") or ""}


def make_llm_decide(orch: Orchestrator, cfg: HarnessConfig, *, mcp_servers=None,
                    template=None):
    """엔진 decide_fn 팩토리. 결정마다 fresh ORCHESTRATOR instance를 spawn한다.

    반환하는 `decide`는 엔진의 decide_fn 계약(finding #5/#6)을 따른다:
    `async (trigger, snapshot) -> (decision: dict, producer_instance_id: str,
    usage_tokens: int)`. producer_instance_id는 이 결정을 생산한 fresh ORCHESTRATOR
    instance_id — 엔진이 DecisionEvent를 그 instance에 정확히 귀속시키는 데 쓴다.
    usage_tokens는 start_worker의 최초 turn(discard됐던 usage, adapter.initial_usage로
    복구)과 이후 send() turn(들)의 usage 합계 — 엔진이 실행 전체 토큰 guard에 합산한다.
    """
    tmpl = template or DEFAULT_TEMPLATE
    tier = cfg.role_defaults[Role.ORCHESTRATOR].tier

    async def decide(trigger: str, snapshot: dict) -> tuple[dict, str, int]:
        inst = await orch.spawn(Role.ORCHESTRATOR, tier,
                                execution_id=snapshot["execution_id"],
                                node_id=f"decision-{trigger.lower()}",
                                task_scope="workflow decision", worktree=None)
        msg = ("다음 스냅샷을 근거로 결정을 내려라.\n```json\n"
               + json.dumps(snapshot, ensure_ascii=False, indent=1) + "\n```")
        sid = await orch.start_worker(inst, msg, mcp_servers=mcp_servers)
        adapter = orch.adapters[inst.provider]
        usage_tokens = 0
        initial = await adapter.initial_usage(sid)
        if initial is not None:
            usage_tokens += _tokens(initial)
        out = await adapter.send(sid, "결정을 스키마대로 제출해.")
        usage_tokens += _tokens(out.usage)
        try:
            return validate_decision(out.structured, trigger, tmpl), inst.instance_id, usage_tokens
        except DecisionError as e:
            retry = await adapter.send(
                sid, f"직전 결정이 거부됐다({e}). allowed_actions "
                     f"{ALLOWED_BY_TRIGGER[trigger]} 중에서만 골라 다시 제출해.")
            usage_tokens += _tokens(retry.usage)
            try:
                return (validate_decision(retry.structured, trigger, tmpl),
                        inst.instance_id, usage_tokens)
            except DecisionError as e2:
                return ({"action": "ASK_USER", "target_node": None,
                         "rationale": f"invalid decision after retry: {e2}"},
                        inst.instance_id, usage_tokens)
    return decide
