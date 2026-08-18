"""§10 Stateful Workflow — Python 선언 템플릿과 결정적 전이 규칙 (spec 결정 1·2).

정책이 답을 정해둔 전이는 next_step()이 처리하고, 나머지는 결정 지점
(ALLOWED_BY_TRIGGER의 트리거)으로 넘긴다.
"""
from __future__ import annotations

from dataclasses import dataclass

from .schema import Role

DECISION_ACTIONS = ["PROCEED", "RETRY_NODE", "ESCALATE_MODEL", "SKIP_NODE",
                    "REPLAN", "ASK_USER", "ABORT"]

# 트리거별 허용 action — 엔진이 LLM 결정을 이 목록과 대조 검증한다 (spec 결정 3)
ALLOWED_BY_TRIGGER: dict[str, list[str]] = {
    "CLASSIFY": ["PROCEED", "SKIP_NODE"],
    "NEED_REPLAN": ["REPLAN", "ASK_USER", "ABORT"],
    "BLOCKED": ["RETRY_NODE", "ESCALATE_MODEL", "ASK_USER", "ABORT"],
    "INSUFFICIENT_CAPABILITY": ["ESCALATE_MODEL", "ASK_USER", "ABORT"],
    "LOOP_GUARD_EXCEEDED": ["REPLAN", "ESCALATE_MODEL", "ASK_USER", "ABORT"],
}


class WorkflowError(Exception):
    pass


@dataclass(frozen=True)
class NodeSpec:
    node_id: str
    role: Role
    message: str                      # 노드 최초 투입 메시지, {task} placeholder 지원
    conditional: bool = False         # CLASSIFY 결정으로 생략 가능
    loop_back_to: str | None = None   # NOT_PASS 시 복귀 노드 (None=자기 자신 재시도)


@dataclass(frozen=True)
class WorkflowTemplate:
    template_id: str
    nodes: tuple[NodeSpec, ...]

    def __post_init__(self):
        ids = [n.node_id for n in self.nodes]
        if not ids:
            raise WorkflowError(f"{self.template_id}: empty template")
        if len(set(ids)) != len(ids):
            raise WorkflowError(f"{self.template_id}: duplicate node ids")
        for n in self.nodes:
            if n.loop_back_to is not None and n.loop_back_to not in ids:
                raise WorkflowError(
                    f"{self.template_id}: {n.node_id} loops back to unknown node {n.loop_back_to}")

    def node(self, node_id: str) -> NodeSpec:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        raise WorkflowError(f"{self.template_id}: unknown node {node_id}")

    def index(self, node_id: str) -> int:
        for i, n in enumerate(self.nodes):
            if n.node_id == node_id:
                return i
        raise WorkflowError(f"{self.template_id}: unknown node {node_id}")


DEFAULT_TEMPLATE = WorkflowTemplate("default-v1", (
    NodeSpec("explore", Role.EXPLORER, "다음 작업을 위한 사전 조사를 수행해 보고해: {task}",
             conditional=True),
    NodeSpec("develop", Role.DEVELOPER, "다음 작업을 구현하고 확인 후 보고해: {task}"),
    NodeSpec("review", Role.REVIEWER,
             "직전 Developer의 변경(git diff HEAD)을 검토 판정해. 작업: {task}",
             loop_back_to="develop"),
    NodeSpec("qa", Role.QA, "다음 작업의 acceptance를 검증해 보고해: {task}",
             conditional=True, loop_back_to="develop"),
))


@dataclass(frozen=True)
class Step:
    kind: str                   # ADVANCE | LOOP | DECIDE
    target: str | None = None   # LOOP: 복귀 node_id
    trigger: str | None = None  # DECIDE: ALLOWED_BY_TRIGGER의 key


def next_step(node: NodeSpec, transition: str) -> Step:
    """consume_result 전이값 → 엔진 스텝. §10.1/§10.2 표의 코드화."""
    if transition == "PASS":
        return Step("ADVANCE")
    if transition == "NOT_PASS":
        return Step("LOOP", target=node.loop_back_to or node.node_id)
    if transition in ("NEED_REPLAN", "BLOCKED", "INSUFFICIENT_CAPABILITY"):
        return Step("DECIDE", trigger=transition)
    raise WorkflowError(f"unknown transition {transition!r} at node {node.node_id}")
