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


LEADER_INTRO = (
    "너는 이 작업 스레드의 crew leader다. 앞으로 결정 지점마다 스냅샷이 주어진다. "
    "이전 결정과 그때 확정된 사실을 기억한 채 일관되게 결정하라.")
COMPACT_MSG = (
    "컨텍스트를 정리한다. 지금까지 내린 결정, 확정된 사실, 아직 열린 쟁점을 "
    "summary 필드에 15줄 이내로 압축해 보고해라 (decision.action은 ASK_USER, "
    "rationale은 '컨텍스트 압축'으로 둔다). 이 요약만 다음 세션에 인계된다.")


def _tokens(u: Usage) -> int:
    return (u.input_tokens or 0) + (u.output_tokens or 0)


def _context_used(u: Usage) -> int:
    """이 turn이 실제로 점유한 컨텍스트 창 크기 추정.

    resume된 세션은 대화 전체가 매 turn의 입력이 된다 — 캐시된 prefix는
    cache_read/cache_creation(Claude) 또는 cached_input(Codex)로 분리 보고되므로
    창 점유량은 그 합계 + 신규 입력 + 출력이다. 누적 과금 토큰(_tokens)과 달리
    이 값이 compaction 시점 판단의 근거다.
    """
    return ((u.input_tokens or 0) + (u.output_tokens or 0)
            + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0)
            + (u.cached_input_tokens or 0))


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
                    template=None, leader_state: dict | None = None,
                    compact_at: int | None = None):
    """엔진 decide_fn 팩토리. 결정마다 fresh ORCHESTRATOR instance를 spawn한다.

    반환하는 `decide`는 엔진의 decide_fn 계약(finding #5/#6)을 따른다:
    `async (trigger, snapshot) -> (decision: dict, producer_instance_id: str,
    usage_tokens: int)`. producer_instance_id는 이 결정을 생산한 fresh ORCHESTRATOR
    instance_id — 엔진이 DecisionEvent를 그 instance에 정확히 귀속시키는 데 쓴다.
    usage_tokens는 start_worker의 최초 turn(discard됐던 usage, adapter.initial_usage로
    복구)과 이후 send() turn(들)의 usage 합계 — 엔진이 실행 전체 토큰 guard에 합산한다.

    `leader_state`(dict)를 넘기면 결정 세션을 **유지**한다: 결정마다 fresh spawn하는
    대신 한 세션에 스냅샷을 이어 보내 이전 결정의 맥락을 그대로 들고 판단한다.
    `compact_at`(토큰)을 함께 넘기면 그 세션의 컨텍스트 점유량이 임계치에 닿을 때
    leader가 스스로 요약하게 하고, 그 요약만 seed로 새 세션을 열어 창을 비운다
    (compaction). `leader_state`가 None이면 종전대로 결정마다 fresh 세션이다.
    """
    tmpl = template or DEFAULT_TEMPLATE
    tier = cfg.role_defaults[Role.ORCHESTRATOR].tier

    async def _open(execution_id: str, trigger: str, first_msg: str, seed: str | None):
        intro = LEADER_INTRO if leader_state is not None else ""
        if seed:
            intro += f"\n\n[이전 컨텍스트 요약 — 이어서 판단하라]\n{seed}"
        inst = await orch.spawn(Role.ORCHESTRATOR, tier, execution_id=execution_id,
                                node_id=f"decision-{trigger.lower()}",
                                task_scope="workflow decision", worktree=None)
        msg = f"{intro}\n\n{first_msg}" if intro else first_msg
        sid = await orch.start_worker(inst, msg, mcp_servers=mcp_servers)
        return inst, sid

    async def _compact(execution_id: str) -> int:
        """leader 세션의 컨텍스트를 요약으로 접는다. 소비한 토큰을 반환."""
        inst, sid = leader_state["inst"], leader_state["sid"]
        adapter = orch.adapters[inst.provider]
        spent = 0
        seed = leader_state.get("seed") or ""
        try:
            out = await adapter.send(sid, COMPACT_MSG)
            spent += _tokens(out.usage)
            seed = str((out.structured or {}).get("summary") or out.text or seed)
        except Exception as e:      # 요약 실패해도 세션 교체는 진행 (창 초과가 더 위험)
            seed = f"{seed}\n(요약 실패: {e!r})"
        orch.trace.append("LeaderCompactEvent", task_id=execution_id,
                          execution_id=execution_id, instance_id=inst.instance_id,
                          payload={"context_used": leader_state.get("context_used", 0),
                                   "compact_at": compact_at, "seed_chars": len(seed)})
        leader_state.update({"inst": None, "sid": None, "context_used": 0, "seed": seed})
        return spent

    async def decide(trigger: str, snapshot: dict) -> tuple[dict, str, int]:
        execution_id = snapshot["execution_id"]
        msg = ("다음 스냅샷을 근거로 결정을 내려라.\n```json\n"
               + json.dumps(snapshot, ensure_ascii=False, indent=1) + "\n```")
        usage_tokens = 0

        if leader_state is None:                       # 종전 동작: 결정마다 fresh
            inst, sid = await _open(execution_id, trigger, msg, None)
            adapter = orch.adapters[inst.provider]
            initial = await adapter.initial_usage(sid)
            if initial is not None:
                usage_tokens += _tokens(initial)
        else:
            if (leader_state.get("sid") is not None and compact_at
                    and leader_state.get("context_used", 0) >= compact_at):
                usage_tokens += await _compact(execution_id)
            if leader_state.get("sid") is None:
                inst, sid = await _open(execution_id, trigger, msg,
                                        leader_state.get("seed"))
                leader_state.update({"inst": inst, "sid": sid})
                adapter = orch.adapters[inst.provider]
                initial = await adapter.initial_usage(sid)
                if initial is not None:
                    usage_tokens += _tokens(initial)
                    leader_state["context_used"] = _context_used(initial)
            else:
                inst, sid = leader_state["inst"], leader_state["sid"]
                adapter = orch.adapters[inst.provider]
                nudge = await adapter.send(sid, msg)   # 유지 세션엔 스냅샷을 이어 보낸다
                usage_tokens += _tokens(nudge.usage)
                leader_state["context_used"] = _context_used(nudge.usage)

        out = await adapter.send(sid, "결정을 스키마대로 제출해.")
        usage_tokens += _tokens(out.usage)
        if leader_state is not None:
            leader_state["context_used"] = max(leader_state.get("context_used", 0),
                                               _context_used(out.usage))
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
