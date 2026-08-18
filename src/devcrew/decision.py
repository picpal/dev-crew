"""LLM 결정 지점 (spec 결정 3·4) — fresh ORCHESTRATOR 세션 + 구조화 출력 = 결정."""
from __future__ import annotations

import json

from .config import HarnessConfig
from .orchestrator import Orchestrator
from .schema import Role
from .workflow import ALLOWED_BY_TRIGGER, DEFAULT_TEMPLATE, WorkflowError, WorkflowTemplate


class DecisionError(Exception):
    pass


def validate_decision(structured, trigger: str, template: WorkflowTemplate) -> dict:
    """구조화 출력 → 검증된 decision dict. 허용 밖·malformed는 DecisionError (fail-closed)."""
    if not isinstance(structured, dict):
        raise DecisionError(f"malformed decision output: {structured!r}")
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
    """엔진 decide_fn 팩토리. 결정마다 fresh ORCHESTRATOR instance를 spawn한다."""
    tmpl = template or DEFAULT_TEMPLATE
    tier = cfg.role_defaults[Role.ORCHESTRATOR].tier

    async def decide(trigger: str, snapshot: dict) -> dict:
        inst = await orch.spawn(Role.ORCHESTRATOR, tier,
                                execution_id=snapshot["execution_id"],
                                node_id=f"decision-{trigger.lower()}",
                                task_scope="workflow decision", worktree=None)
        msg = ("다음 스냅샷을 근거로 결정을 내려라.\n```json\n"
               + json.dumps(snapshot, ensure_ascii=False, indent=1) + "\n```")
        sid = await orch.start_worker(inst, msg, mcp_servers=mcp_servers)
        adapter = orch.adapters[inst.provider]
        out = await adapter.send(sid, "결정을 스키마대로 제출해.")
        try:
            return validate_decision(out.structured, trigger, tmpl)
        except DecisionError as e:
            retry = await adapter.send(
                sid, f"직전 결정이 거부됐다({e}). allowed_actions "
                     f"{ALLOWED_BY_TRIGGER[trigger]} 중에서만 골라 다시 제출해.")
            try:
                return validate_decision(retry.structured, trigger, tmpl)
            except DecisionError as e2:
                return {"action": "ASK_USER", "target_node": None,
                        "rationale": f"invalid decision after retry: {e2}"}
    return decide
