"""harness MCP 서버 — ORCHESTRATOR 결정 세션 전용 읽기 전용 tool 3종 (Task 6).

ORCHESTRATOR는 repo tool을 전혀 갖지 않는다(ROLE_POLICY) — 대신 이 MCP 서버로 trace
상태를 조회해 결정을 내린다. 모든 tool은 조회만 한다: TraceStore.append를 호출하지
않는다(§5 constraint — 결정 세션이 trace를 오염시킬 수 없다).
"""
from __future__ import annotations

import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from .store.trace import TraceStore


def _text(payload) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _error(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}]}


def _handlers(trace: TraceStore, engine=None) -> dict:
    """tool name -> async handler(args: dict) -> dict. SDK 서버 기동 없이 직접 호출 가능한
    테스트 후크 (build_harness_mcp이 이 dict를 @tool로 감싼다)."""

    async def get_execution_state(args: dict) -> dict:
        execution_id = args.get("execution_id")
        transitions = trace.events(event_type="NodeTransitionEvent", execution_id=execution_id)
        decisions = trace.events(event_type="DecisionEvent", execution_id=execution_id)
        if not transitions and not decisions:
            return _error(f"execution not found: {execution_id!r}")

        # node_id별 최근 전이만 유지 (오래된 순으로 순회하므로 마지막 값이 최신)
        nodes: dict[str, dict] = {}
        for e in transitions:
            p = e["payload"]
            nodes[p["node_id"]] = {"node_id": p["node_id"], "transition": p["transition"],
                                   "step_kind": p["step_kind"], "iteration": p["iteration"],
                                   "ts": e["ts"]}
        state = {
            "execution_id": execution_id,
            "nodes": list(nodes.values()),
            "decisions": [{"trigger": e["payload"]["trigger"],
                          "decision": e["payload"]["decision"], "ts": e["ts"]}
                         for e in decisions],
            "last_event_at": max([e["ts"] for e in transitions + decisions], default=None),
        }
        # engine이 주어지고 이 execution의 live 상태를 노출하면 trace 합성 위에 덮어쓴다
        # (WorkflowEngine에는 아직 그런 API가 없다 — 향후 라이브 조회 추가를 위한 훅).
        live = getattr(engine, "live_state", None) if engine is not None else None
        if callable(live):
            live_state = live(execution_id)
            if live_state:
                state.update(live_state)
        return _text(state)

    async def get_worker_result(args: dict) -> dict:
        execution_id = args.get("execution_id")
        instance_id = args.get("instance_id")
        events = trace.events(event_type="WorkerResultEvent", execution_id=execution_id)
        if instance_id:
            events = [e for e in events if e["instance_id"] == instance_id]
        if not events:
            return _error(f"worker result not found: execution_id={execution_id!r} "
                          f"instance_id={instance_id!r}")
        return _text(events[-1]["payload"]["structured"])

    async def get_trace_events(args: dict) -> dict:
        execution_id = args.get("execution_id")
        event_type = args.get("event_type")
        limit = int(args.get("limit") or 50)
        events = trace.events(event_type=event_type, execution_id=execution_id)
        return _text(events[-limit:])

    return {"get_execution_state": get_execution_state,
            "get_worker_result": get_worker_result,
            "get_trace_events": get_trace_events}


_TOOL_SPECS = {
    "get_execution_state": (
        "execution의 최근 노드 전이와 결정 이력을 합성한 상태를 반환한다 (읽기 전용).",
        {"type": "object", "properties": {"execution_id": {"type": "string"}},
         "required": ["execution_id"]},
    ),
    "get_worker_result": (
        "WorkerResultEvent의 structured 결과를 조회한다 (읽기 전용).",
        {"type": "object", "properties": {"execution_id": {"type": "string"},
                                          "instance_id": {"type": "string"}},
         "required": ["execution_id"]},
    ),
    "get_trace_events": (
        "trace 이벤트를 execution_id/event_type/limit으로 필터링해 조회한다 (읽기 전용).",
        {"type": "object", "properties": {"execution_id": {"type": "string"},
                                          "event_type": {"type": "string"},
                                          "limit": {"type": "integer"}},
         "required": ["execution_id"]},
    ),
}


def build_harness_mcp(trace: TraceStore, engine=None):
    """SDK MCP 서버(name="harness") 구성 — ROLE_POLICY의 mcp__harness__* 3종과 짝을 맞춘다."""
    handlers = _handlers(trace, engine)
    tools = [
        tool(name, description, input_schema)(handlers[name])
        for name, (description, input_schema) in _TOOL_SPECS.items()
    ]
    return create_sdk_mcp_server("harness", tools=tools)
