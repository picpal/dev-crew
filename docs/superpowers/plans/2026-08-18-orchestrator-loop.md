# Orchestrator Workflow Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** §10 상태 머신의 결정적 워크플로 엔진 + LLM 결정 지점 + Orchestrator MCP 조회 tool을 구현한다.

**Architecture:** 엔진(`engine.py`)이 Python 선언 템플릿(`workflow.py`)의 규정된 전이를 0-토큰으로 처리하고, 열거된 결정 지점에서만 fresh ORCHESTRATOR 세션(`decision.py`)을 호출한다. 결정은 구조화 출력이며, MCP는 읽기 전용 조회 3종이다. 모든 결과는 생산자 instance로 consume해 attribution 결함(#18 park)을 해소한다.

**Tech Stack:** Python 3.12, claude-agent-sdk 0.2.139 (`create_sdk_mcp_server`, `tool`, `mcp_servers`), 기존 devcrew 모듈 (orchestrator/roles/config/enforcement/trace).

**Spec:** `docs/superpowers/specs/2026-08-18-orchestrator-loop-design.md`

## Global Constraints

- 오류는 fail-fast(설정·번들 누락 즉시 예외), 신뢰 불가 출력은 fail-closed — 조용한 기본값 금지
- live LLM 호출은 p09만, CHEAP/CODEX_DEFAULT tier + 짧은 프롬프트. unit/시뮬은 FakeAdapter만
- 신규 스키마(`roles/orchestrator/output.schema.json`)는 기존 4 role과 동일한 strict 규칙: 모든 object의 `required`가 전체 property key 포함, optionality는 nullable 타입으로
- trace 기록은 `TraceStore.append`(append-only)만 사용, 이벤트 payload에 프롬프트 원문 금지 (structured 결과·요약은 허용)
- 기존 테스트 suite green 유지 (`uv run pytest -q`); `run_review_loop` 제거 시 해당 테스트는 엔진 테스트로 대체
- secrets 없음 — 이 effort는 API key 외 자격증명을 다루지 않는다
- SDK 옵션명·시그니처는 구현 시 `inspect`로 검증 후 사용 (T7 방식)

---

### Task 1: config — loopPolicy + ORCHESTRATOR roleDefaults

**Files:**
- Modify: `config/harness.yaml`
- Modify: `src/devcrew/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `LoopPolicy(max_iterations: int, max_duration_minutes: int, max_token_budget: int, same_finding_escalation_threshold: int)`, `HarnessConfig.loop_policy: LoopPolicy`, `cfg.role_defaults[Role.ORCHESTRATOR].tier == "HIGH_CAPABILITY"`

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_config.py`에 추가:

```python
def test_loop_policy_loaded():
    cfg = config.load()
    assert cfg.loop_policy.max_iterations == 5
    assert cfg.loop_policy.max_duration_minutes == 60
    assert cfg.loop_policy.max_token_budget == 300000
    assert cfg.loop_policy.same_finding_escalation_threshold == 3


def test_loop_policy_missing_fails(tmp_path):
    p = tmp_path / "h.yaml"
    p.write_text(VALID_YAML_WITHOUT_LOOP_POLICY)  # 기존 fixture 재사용해 loopPolicy 절만 제거
    with pytest.raises(config.ConfigError, match="loopPolicy"):
        config.load(p)


def test_loop_policy_nonpositive_fails(tmp_path):
    # maxIterations: 0 으로 쓴 yaml → ConfigError
    ...


def test_orchestrator_role_default_required():
    cfg = config.load()
    assert cfg.role_defaults[Role.ORCHESTRATOR].tier == "HIGH_CAPABILITY"
```

기존 fixture 헬퍼(`tests/test_config.py`의 valid yaml 생성부)를 재사용하고, ORCHESTRATOR가 이제 필수이므로 기존 "필수 role 누락" 테스트의 기대 목록을 갱신한다.

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/test_config.py -q` → 신규 테스트 FAIL
- [ ] **Step 3: 구현**

`config/harness.yaml`에 추가:

```yaml
roleDefaults:
  ORCHESTRATOR: { tier: HIGH_CAPABILITY }
  # ... 기존 항목 유지

# §10.3 loop guard — Task 실패가 아니라 결정 지점 진입 조건
loopPolicy:
  maxIterations: 5
  maxDurationMinutes: 60
  maxTokenBudget: 300000
  sameFindingEscalationThreshold: 3
```

`src/devcrew/config.py`:

```python
@dataclass(frozen=True)
class LoopPolicy:
    max_iterations: int
    max_duration_minutes: int
    max_token_budget: int
    same_finding_escalation_threshold: int


@dataclass(frozen=True)
class HarnessConfig:
    tiers: dict[str, TierSpec]
    role_defaults: dict[Role, RoleDefault]
    loop_policy: LoopPolicy
```

`load()`에서 `REQUIRED_ROLE_DEFAULTS = frozenset(Role)`로 변경(ORCHESTRATOR 주석 삭제), loopPolicy 파싱 추가:

```python
    raw_lp = raw.get("loopPolicy")
    if not raw_lp:
        raise ConfigError(f"config has no loopPolicy: {p}")
    try:
        lp = LoopPolicy(int(raw_lp["maxIterations"]), int(raw_lp["maxDurationMinutes"]),
                        int(raw_lp["maxTokenBudget"]), int(raw_lp["sameFindingEscalationThreshold"]))
    except (KeyError, ValueError, TypeError) as e:
        raise ConfigError(f"invalid loopPolicy {p}: {e}") from e
    if min(lp.max_iterations, lp.max_duration_minutes, lp.max_token_budget,
           lp.same_finding_escalation_threshold) <= 0:
        raise ConfigError(f"loopPolicy values must be positive: {p}")
```

- [ ] **Step 4: 통과 확인** — `uv run pytest tests/test_config.py -q` PASS, 전체 suite green
- [ ] **Step 5: Commit** — `git add config/harness.yaml src/devcrew/config.py tests/test_config.py && git commit -m "feat: loopPolicy config + ORCHESTRATOR roleDefaults"`

---

### Task 2: Orchestrator role bundle

**Files:**
- Create: `roles/orchestrator/prompt.md`
- Create: `roles/orchestrator/output.schema.json`
- Test: `tests/test_roles.py`

**Interfaces:**
- Produces: `load_bundle(Role.ORCHESTRATOR)` 동작. 스키마: `{status, summary, decision:{action, target_node, rationale}}`

- [ ] **Step 1: 실패 테스트** — `tests/test_roles.py`에 추가 (기존 `_assert_strict` 재귀 헬퍼 재사용):

```python
def test_orchestrator_bundle_loads():
    b = load_bundle(Role.ORCHESTRATOR)
    props = b.schema["properties"]
    d = props["decision"]["properties"]
    assert d["action"]["enum"] == ["PROCEED", "RETRY_NODE", "ESCALATE_MODEL",
                                  "SKIP_NODE", "REPLAN", "ASK_USER", "ABORT"]
    assert d["target_node"]["type"] == ["string", "null"]
    _assert_strict(b.schema)
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/test_roles.py -q` FAIL
- [ ] **Step 3: 번들 작성**

`roles/orchestrator/output.schema.json`:

```json
{
  "type": "object",
  "properties": {
    "status": {"type": "string",
               "enum": ["PASS", "NOT_PASS", "NEED_REPLAN", "BLOCKED", "INSUFFICIENT_CAPABILITY"]},
    "summary": {"type": "string"},
    "decision": {
      "type": "object",
      "properties": {
        "action": {"type": "string",
                   "enum": ["PROCEED", "RETRY_NODE", "ESCALATE_MODEL", "SKIP_NODE",
                            "REPLAN", "ASK_USER", "ABORT"]},
        "target_node": {"type": ["string", "null"]},
        "rationale": {"type": "string"}
      },
      "required": ["action", "target_node", "rationale"],
      "additionalProperties": false
    }
  },
  "required": ["status", "summary", "decision"],
  "additionalProperties": false
}
```

`roles/orchestrator/prompt.md` — 기존 4 role과 같은 한국어 4절 구성(책임 / 금지 / 작업 방식 / 보고 규칙). 핵심 내용:
- 책임: 워크플로 결정 지점에서 스냅샷을 근거로 단일 결정(decision)을 내린다. Control Plane이며 구현·조사·리뷰를 직접 수행하지 않는다.
- 금지: repo 파일 접근·수정, 허용 목록(`allowed_actions`) 밖 action 선택, 스냅샷에 없는 사실의 추정 단정
- 작업 방식: 스냅샷의 `trigger`·`worker_result`·`history`를 먼저 읽고, 부족하면 harness MCP 조회 tool(get_execution_state, get_worker_result, get_trace_events)로 보강한 뒤 결정한다. 결정 근거는 rationale에 1–3문장.
- 보고 규칙: `status`는 결정 절차 수행 여부(정상 결정=PASS, 스냅샷 불충분으로 결정 불가=BLOCKED). `decision.action`은 반드시 스냅샷의 `allowed_actions` 중 하나. `target_node`는 RETRY_NODE/SKIP_NODE/REPLAN일 때 대상 node_id, 그 외 null.

- [ ] **Step 4: 통과 확인** — `uv run pytest tests/test_roles.py -q` PASS
- [ ] **Step 5: Commit** — `git add roles/orchestrator tests/test_roles.py && git commit -m "feat: orchestrator role bundle (decision schema + prompt)"`

---

### Task 3: workflow.py — 템플릿 + 전이 규칙 + 결정 상수

**Files:**
- Create: `src/devcrew/workflow.py`
- Test: `tests/test_workflow.py`

**Interfaces:**
- Produces: `NodeSpec(node_id, role, message, conditional=False, loop_back_to=None)`, `WorkflowTemplate(template_id, nodes)` (+ `.node(id)`, `.index(id)`), `DEFAULT_TEMPLATE`, `Step(kind, target, trigger)`, `next_step(node, transition) -> Step`, `DECISION_ACTIONS`, `ALLOWED_BY_TRIGGER`, `WorkflowError`

- [ ] **Step 1: 실패 테스트** — `tests/test_workflow.py`:

```python
import pytest
from devcrew.schema import Role
from devcrew.workflow import (ALLOWED_BY_TRIGGER, DECISION_ACTIONS, DEFAULT_TEMPLATE,
                              NodeSpec, Step, WorkflowError, WorkflowTemplate, next_step)


def test_default_template_shape():
    ids = [n.node_id for n in DEFAULT_TEMPLATE.nodes]
    assert ids == ["explore", "develop", "review", "qa"]
    assert DEFAULT_TEMPLATE.node("explore").conditional
    assert DEFAULT_TEMPLATE.node("review").loop_back_to == "develop"
    assert DEFAULT_TEMPLATE.node("qa").loop_back_to == "develop"
    assert DEFAULT_TEMPLATE.index("review") == 2


def test_template_validation():
    n = NodeSpec("a", Role.DEVELOPER, "m: {task}")
    with pytest.raises(WorkflowError):
        WorkflowTemplate("t", ())                       # empty
    with pytest.raises(WorkflowError):
        WorkflowTemplate("t", (n, n))                   # duplicate id
    with pytest.raises(WorkflowError):
        WorkflowTemplate("t", (NodeSpec("a", Role.QA, "m", loop_back_to="nope"),))


@pytest.mark.parametrize("transition,kind", [
    ("PASS", "ADVANCE"), ("NOT_PASS", "LOOP"), ("NEED_REPLAN", "DECIDE"),
    ("BLOCKED", "DECIDE"), ("INSUFFICIENT_CAPABILITY", "DECIDE")])
def test_next_step_full_table(transition, kind):
    node = DEFAULT_TEMPLATE.node("review")
    s = next_step(node, transition)
    assert s.kind == kind
    if kind == "LOOP":
        assert s.target == "develop"
    if kind == "DECIDE":
        assert s.trigger == transition


def test_next_step_self_loop_when_no_loop_back():
    s = next_step(DEFAULT_TEMPLATE.node("develop"), "NOT_PASS")
    assert s == Step("LOOP", target="develop")


def test_next_step_unknown_transition():
    with pytest.raises(WorkflowError):
        next_step(DEFAULT_TEMPLATE.node("develop"), "WAT")


def test_allowed_by_trigger_actions_are_known():
    for trigger, actions in ALLOWED_BY_TRIGGER.items():
        assert actions and set(actions) <= set(DECISION_ACTIONS)
    assert set(ALLOWED_BY_TRIGGER) == {"CLASSIFY", "NEED_REPLAN", "BLOCKED",
                                       "INSUFFICIENT_CAPABILITY", "LOOP_GUARD_EXCEEDED"}
```

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/test_workflow.py -q` FAIL (module 없음)
- [ ] **Step 3: 구현** — `src/devcrew/workflow.py`:

```python
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
```

- [ ] **Step 4: 통과 확인** — `uv run pytest tests/test_workflow.py -q` PASS
- [ ] **Step 5: Commit** — `git add src/devcrew/workflow.py tests/test_workflow.py && git commit -m "feat: workflow templates + deterministic transition rules"`

---

### Task 4: WorkflowEngine — 노드 실행·loop guard·attribution

**Files:**
- Create: `src/devcrew/engine.py`
- Modify: `src/devcrew/orchestrator.py` (consume_result payload 확장, run_review_loop·LoopResult 제거)
- Modify: `tests/test_orchestrator.py` (run_review_loop 테스트 제거 — 엔진 테스트가 대체)
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `Orchestrator.spawn/start_worker/consume_result`, `FakeAdapter(structured_script=...)`, `HarnessConfig.loop_policy`, T3의 workflow API
- Produces: `WorkflowEngine(orch, cfg, *, template=DEFAULT_TEMPLATE, decide_fn=None, clock=time.monotonic)`, `await engine.run(execution_id=..., task=..., worktree=None) -> ExecutionResult(status, node_history, decisions, total_tokens)`; status ∈ `COMPLETED | NEEDS_HUMAN | ABORTED`. `consume_result`의 `WorkerResultEvent` payload에 `structured` 전문 포함

핵심 설계 (구현 지침):

```python
"""결정적 워크플로 엔진 (spec 결정 1·6·7).

- 규정된 전이는 즉시 처리, DECISION 트리거에서만 decide_fn 호출
- 모든 TurnOutcome은 그 결과를 생산한 instance로 consume_result에 전달 (attribution)
- decide_fn: async (trigger: str, snapshot: dict) -> dict  (decision dict 반환).
  None이면 모든 결정 지점에서 {"action": "ASK_USER", ...}로 처리 (LLM 없는 안전 기본값)
"""
import time
from dataclasses import dataclass, field

from .adapters.base import TurnOutcome
from .config import HarnessConfig
from .schema import AgentInstance, Role, Usage
from .workflow import (ALLOWED_BY_TRIGGER, DEFAULT_TEMPLATE, NodeSpec, Step,
                       WorkflowError, WorkflowTemplate, next_step)

# §7.6 MODEL_ESCALATION 사다리 — 엔진 init에서 cfg.tiers 존재 검증
ESCALATION_LADDER = {"CHEAP": "DEFAULT", "DEFAULT": "HIGH_CAPABILITY",
                     "CODEX_DEFAULT": "CODEX_HIGH_REASONING"}

REPORT_MSG = "이제 최종 보고를 스키마대로 제출해."


def _tokens(u: Usage) -> int:
    return (u.input_tokens or 0) + (u.output_tokens or 0)


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
```

`run()` 알고리즘 (이 순서대로 구현):

1. `nodes = [NodeRuntime(spec=n, tier=cfg.role_defaults[n.role].tier) for n in template.nodes]`
2. 조건부 노드가 있으면 `CLASSIFY` 결정: `SKIP_NODE`면 해당 노드 `skipped=True` (target이 conditional 노드가 아니면 invalid 결정으로 간주 → `ASK_USER` 강등). `PROCEED`면 전부 실행.
3. 메인 루프 — `idx`로 순회, skipped는 건너뜀:
   - `outcome = await self._run_node(rt, execution_id, task, worktree, follow_up_msg)`
     - 최초 방문: `inst = await orch.spawn(rt.spec.role, rt.tier, execution_id=..., node_id=rt.spec.node_id, task_scope=task, worktree=worktree)` → `sid = await orch.start_worker(inst, rt.spec.message.format(task=task))` → `outcome = await adapter.send(sid, REPORT_MSG)` (p08 검증 패턴)
     - 재방문(loop-back): `outcome = await adapter.send(rt.session_id, follow_up_msg)` — follow_up_msg는 직전 결과의 `structured` 전문을 JSON으로 붙인 "위 결과를 반영해 수정/재수행 후 스키마대로 최종 보고해."
   - `total_tokens += _tokens(outcome.usage)`
   - `transition = orch.consume_result(rt.inst, outcome)` — **반드시 생산자 instance** (Reviewer 결과는 review 노드의 inst로 consume — dev_inst로 기록하던 #18 결함이 여기서 해소된다. REVIEWER role이면 consume_result 내부 verdict 규칙이 `inst.role`로 자동 적용되므로 `as_role` 불필요)
   - `NodeTransitionEvent` 기록: payload `{node_id, transition, step_kind, iteration}`
   - `next_step()` 분기:
     - ADVANCE → `idx += 1`
     - LOOP → target rt의 `iterations += 1`; same-finding 시그니처 `(node_id, structured.get("summary"))` 연속 카운트; **guard 검사** (아래) 후 초과 없으면 `idx = template.index(step.target)`, follow_up_msg 구성
     - DECIDE → 결정 지점 (아래 5)
4. guard 검사 (LOOP에서만): `rt.iterations >= lp.max_iterations` or `same_count >= lp.same_finding_escalation_threshold` or `total_tokens > lp.max_token_budget` or `(clock() - started) > lp.max_duration_minutes * 60` → trigger `LOOP_GUARD_EXCEEDED`로 결정 지점 진입 (§10.3: 즉시 실패가 아니라 escalation)
5. 결정 지점: `d = await self._decide(trigger, snapshot)` — decide_fn이 None이면 `{"action": "ASK_USER", "target_node": None, "rationale": "no decide_fn configured"}`. `DecisionEvent` 기록 (payload: trigger, decision 전문). action 적용:
   - `PROCEED` → 그대로 계속 (CLASSIFY 전용)
   - `SKIP_NODE` → target 노드 skipped (conditional 검증)
   - `RETRY_NODE` → target(기본: 현재 노드) iterations/same-finding 리셋 후 `idx = index(target)`; follow_up 재시도 메시지
   - `ESCALATE_MODEL` → `ESCALATION_LADDER[rt.tier]` 없으면(최상위) `NEEDS_HUMAN` 종료; 있으면 `new = await orch.spawn(role, next_tier, replaced=rt.inst, escalation_reason=trigger, ...)` → rt 교체, `start_worker` + REPORT_MSG로 재실행 경로 태움 (rt.inst/session 갱신, iterations 리셋)
   - `REPLAN` → `idx = index(target or "develop")`, 전 노드 iterations·same-finding 리셋
   - `ASK_USER` → `NEEDS_HUMAN` 종료 / `ABORT` → `ABORTED` 종료
6. 스냅샷 dict (decision.py와 공유되는 계약):

```python
{"trigger": trigger, "execution_id": execution_id, "task": task,
 "current_node": rt.spec.node_id,
 "nodes": [{"node_id": r.spec.node_id, "role": r.spec.role.value,
            "conditional": r.spec.conditional, "skipped": r.skipped,
            "iterations": r.iterations} for r in nodes],
 "worker_result": last_structured,          # CLASSIFY에선 None
 "history": node_history[-10:],
 "allowed_actions": ALLOWED_BY_TRIGGER[trigger],
 "loop_policy": {"max_iterations": lp.max_iterations, ...}}
```

`orchestrator.py` 변경: `consume_result`의 정상 경로 payload에 `"structured": structured` 추가 (MCP get_worker_result의 데이터 소스). `run_review_loop`/`LoopResult` 삭제 — docstring의 관련 서사도 함께 정리.

- [ ] **Step 1: 실패 테스트** — `tests/test_engine.py`. FakeAdapter 하나를 모든 role에 공유하고 `structured_script`로 시나리오를 구성한다 (스크립트는 send 호출 순서대로 소비됨 — start_worker의 초기 turn은 FakeAdapter.start_session이라 스크립트를 소비하지 않음에 유의):

```python
import pytest
from devcrew.adapters.base import FakeAdapter
from devcrew.config import load as load_config
from devcrew.engine import ExecutionResult, WorkflowEngine
from devcrew.orchestrator import Orchestrator
from devcrew.schema import Provider, Role
from devcrew.workflow import DEFAULT_TEMPLATE, NodeSpec, WorkflowTemplate

# 시뮬용 2-노드 템플릿 (explore/qa 없이 develop→review 루프만)
SIM = WorkflowTemplate("sim", (
    NodeSpec("develop", Role.DEVELOPER, "구현: {task}"),
    NodeSpec("review", Role.REVIEWER, "검토: {task}", loop_back_to="develop"),
))

PASS_DEV = {"status": "PASS", "summary": "done", "changed_files": ["a.py"],
            "build": {"ok": True, "detail": ""}, "tests": {"passed": 1, "failed": 0, "detail": ""}}
PASS_REVIEW = {"status": "PASS", "summary": "ok", "verdict": "PASS", "findings": []}
FAIL_REVIEW = {"status": "PASS", "summary": "issues", "verdict": "NOT_PASS",
               "findings": [{"severity": "major", "file": "a.py", "line": 1, "description": "bug"}]}


def make_engine(structured_script, decide_fn=None, template=SIM, clock=None):
    trace, registry = ...  # 기존 test_orchestrator.py의 in-memory 스토어 fixture 재사용
    fake = FakeAdapter(structured_script=structured_script)
    orch = Orchestrator(trace, registry, {Provider.CLAUDE_CODE: fake, Provider.CODEX: fake})
    cfg = load_config()
    kwargs = {"template": template, "decide_fn": decide_fn}
    if clock:
        kwargs["clock"] = clock
    return WorkflowEngine(orch, cfg, **kwargs), trace, fake


@pytest.mark.asyncio
async def test_straight_pass():
    engine, trace, _ = make_engine([PASS_DEV, PASS_REVIEW])
    r = await engine.run(execution_id="E1", task="t")
    assert r.status == "COMPLETED"
    assert [h["node_id"] for h in r.node_history] == ["develop", "review"]


@pytest.mark.asyncio
async def test_review_loop_then_pass():
    # dev PASS → review NOT_PASS → dev fix PASS → review PASS
    engine, trace, _ = make_engine([PASS_DEV, FAIL_REVIEW, PASS_DEV, PASS_REVIEW])
    r = await engine.run(execution_id="E2", task="t")
    assert r.status == "COMPLETED"
    assert r.node_history[1]["transition"] == "NOT_PASS"


@pytest.mark.asyncio
async def test_attribution_reviewer_result_recorded_under_reviewer_instance():
    engine, trace, _ = make_engine([PASS_DEV, FAIL_REVIEW, PASS_DEV, PASS_REVIEW])
    await engine.run(execution_id="E3", task="t")
    events = trace.events(event_type="WorkerResultEvent")
    rev_events = [e for e in events if e["payload"]["role"] == "REVIEWER"]
    dev_events = [e for e in events if e["payload"]["role"] == "DEVELOPER"]
    assert rev_events and dev_events
    # Reviewer 이벤트의 instance_id는 rev- prefix (spawn의 role 앞 3글자 규칙)
    assert all(e["instance_id"].startswith("rev") for e in rev_events)
    assert all(e["instance_id"].startswith("dev") for e in dev_events)
    # 회귀 고정: reviewer 이벤트가 developer instance로 귀속되지 않는다 (#18 park 해소)
    assert not any(e["instance_id"].startswith("dev") for e in rev_events)


@pytest.mark.asyncio
async def test_same_finding_guard_triggers_decision():
    # 동일 summary의 NOT_PASS 3연속 → LOOP_GUARD_EXCEEDED → decide_fn 호출 → ABORT
    calls = []
    async def decide(trigger, snapshot):
        calls.append(trigger)
        return {"action": "ABORT", "target_node": None, "rationale": "test"}
    script = [PASS_DEV] + [FAIL_REVIEW, PASS_DEV] * 3 + [FAIL_REVIEW]
    engine, trace, _ = make_engine(script, decide_fn=decide)
    r = await engine.run(execution_id="E4", task="t")
    assert r.status == "ABORTED"
    assert calls == ["LOOP_GUARD_EXCEEDED"]
    assert trace.events(event_type="DecisionEvent")


@pytest.mark.asyncio
async def test_max_iterations_guard():
    async def decide(trigger, snapshot):
        return {"action": "ASK_USER", "target_node": None, "rationale": "cap"}
    # summary를 매번 다르게 해 same-finding이 아니라 iteration 한도로 걸리게 한다
    fails = [dict(FAIL_REVIEW, summary=f"issue-{i}") for i in range(6)]
    script = [PASS_DEV]
    for f in fails:
        script += [f, PASS_DEV]
    engine, _, _ = make_engine(script, decide_fn=decide)
    r = await engine.run(execution_id="E5", task="t")
    assert r.status == "NEEDS_HUMAN"


@pytest.mark.asyncio
async def test_token_budget_guard():
    # FakeAdapter는 turn당 output 1토큰 — loop_policy를 직접 낮출 수 없으므로
    # cfg를 dataclasses.replace로 손본 사본을 엔진에 주입한다
    import dataclasses
    from devcrew.config import LoopPolicy
    ...  # cfg.loop_policy = LoopPolicy(999, 999, 2, 999) 로 재구성 → 3turn째 guard


@pytest.mark.asyncio
async def test_need_replan_goes_to_decision():
    async def decide(trigger, snapshot):
        assert snapshot["allowed_actions"] == ["REPLAN", "ASK_USER", "ABORT"]
        return {"action": "ASK_USER", "target_node": None, "rationale": "unclear"}
    engine, _, _ = make_engine([{"status": "NEED_REPLAN", "summary": "?",
                                 "changed_files": [], "build": {"ok": False, "detail": ""},
                                 "tests": {"passed": 0, "failed": 0, "detail": ""}}],
                               decide_fn=decide)
    r = await engine.run(execution_id="E6", task="t")
    assert r.status == "NEEDS_HUMAN"


@pytest.mark.asyncio
async def test_no_decide_fn_defaults_to_needs_human():
    engine, _, _ = make_engine([{"status": "BLOCKED", "summary": "b",
                                 "changed_files": [], "build": {"ok": False, "detail": ""},
                                 "tests": {"passed": 0, "failed": 0, "detail": ""}}])
    r = await engine.run(execution_id="E7", task="t")
    assert r.status == "NEEDS_HUMAN"


@pytest.mark.asyncio
async def test_classify_skips_conditional_nodes():
    # DEFAULT_TEMPLATE + explore/qa 생략 결정 → develop/review만 실행
    async def decide(trigger, snapshot):
        assert trigger == "CLASSIFY"
        return {"action": "SKIP_NODE", "target_node": "explore", "rationale": "simple task"}
    # CLASSIFY는 1회 호출로 1개 노드만 생략 가능 — qa는 남아 실행됨
    pass_qa = {"status": "PASS", "summary": "ok", "plan": ["p"],
               "results": [{"criterion": "c", "ok": True, "repro": None}]}
    engine, _, _ = make_engine([PASS_DEV, PASS_REVIEW, pass_qa],
                               decide_fn=decide, template=DEFAULT_TEMPLATE)
    r = await engine.run(execution_id="E8", task="t")
    assert r.status == "COMPLETED"
    assert [h["node_id"] for h in r.node_history] == ["develop", "review", "qa"]


@pytest.mark.asyncio
async def test_workflow_engine_rejects_unknown_ladder_tier():
    # ESCALATION_LADDER의 tier가 cfg.tiers에 없으면 init에서 WorkflowError
    ...  # cfg 사본에서 tiers를 줄여 검증
```

`WorkerResultEvent` payload의 `structured` 포함도 여기(또는 test_orchestrator.py)에 1개 테스트로 고정한다.

- [ ] **Step 2: 실패 확인** — `uv run pytest tests/test_engine.py -q` FAIL
- [ ] **Step 3: 구현** — 위 설계대로 `engine.py` 작성 + `orchestrator.py`에서 `consume_result` payload 확장·`run_review_loop`/`LoopResult` 제거. `tests/test_orchestrator.py`의 run_review_loop 관련 테스트 삭제(consume_result·spawn 테스트는 유지).
- [ ] **Step 4: 통과 확인** — `uv run pytest -q` 전체 green
- [ ] **Step 5: Commit** — `git add -A src tests && git commit -m "feat: deterministic workflow engine with loop guards + result attribution"`

**주의:** CLASSIFY에서 SKIP_NODE는 1회 결정에 1개 노드다. MVP에선 조건부 노드가 2개(explore, qa)이므로 CLASSIFY 결정을 조건부 노드마다 반복하지 말고, **conditional 노드가 하나라도 있으면 CLASSIFY 1회만 호출**하고 action이 SKIP_NODE면 target 1개만 생략한다. 복수 생략은 후속 (스냅샷에 이 제약을 명시).

---

### Task 5: decision.py — LLM 결정 세션 + 검증·강등

**Files:**
- Create: `src/devcrew/decision.py`
- Test: `tests/test_decision.py`

**Interfaces:**
- Consumes: `Orchestrator.spawn/start_worker`, `adapter.send`, `roles/orchestrator` 번들, T4의 스냅샷 dict 계약
- Produces: `DecisionError`, `validate_decision(structured, trigger, template) -> dict`, `make_llm_decide(orch, cfg, *, mcp_servers=None) -> async (trigger, snapshot) -> dict`

- [ ] **Step 1: 실패 테스트** — `tests/test_decision.py`:

```python
import pytest
from devcrew.decision import DecisionError, make_llm_decide, validate_decision
from devcrew.workflow import DEFAULT_TEMPLATE

GOOD = {"status": "PASS", "summary": "s",
        "decision": {"action": "ABORT", "target_node": None, "rationale": "r"}}


def test_validate_decision_ok():
    d = validate_decision(GOOD, "NEED_REPLAN", DEFAULT_TEMPLATE)
    assert d["action"] == "ABORT"


@pytest.mark.parametrize("bad", [
    None,                                                        # malformed
    {"status": "PASS", "summary": "s"},                          # decision 없음
    {**GOOD, "decision": {"action": "PROCEED", "target_node": None, "rationale": "r"}},  # 허용 밖 action
    {**GOOD, "decision": {"action": "REPLAN", "target_node": "nope", "rationale": "r"}}, # unknown node
    {**GOOD, "status": "BLOCKED"},                               # 결정 미수행
])
def test_validate_decision_rejects(bad):
    with pytest.raises(DecisionError):
        validate_decision(bad, "NEED_REPLAN", DEFAULT_TEMPLATE)


@pytest.mark.asyncio
async def test_llm_decide_retries_once_then_degrades():
    # 1차: 허용 밖 action → 재시도 메시지 전송, 2차: 여전히 invalid → ASK_USER 강등
    # FakeAdapter structured_script: [invalid, invalid]
    ...
    d = await decide("NEED_REPLAN", snapshot)
    assert d["action"] == "ASK_USER"
    assert fake.turns  # 재시도로 send 2회 소비됨을 확인


@pytest.mark.asyncio
async def test_llm_decide_happy_path_spawns_orchestrator():
    # FakeAdapter structured_script: [GOOD] → decision 반환, spawn된 instance role=ORCHESTRATOR,
    # trace의 ModelRoutingEvent에 HIGH_CAPABILITY tier가 기록됨
    ...
```

- [ ] **Step 2: 실패 확인** — FAIL
- [ ] **Step 3: 구현** — `src/devcrew/decision.py`:

```python
"""LLM 결정 지점 (spec 결정 3·4) — fresh ORCHESTRATOR 세션 + 구조화 출력 = 결정."""
from __future__ import annotations

import json

from .config import HarnessConfig
from .orchestrator import Orchestrator
from .schema import Role
from .workflow import ALLOWED_BY_TRIGGER, WorkflowTemplate


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
        template.node(target)   # unknown node → WorkflowError; DecisionError로 감싼다
    return {"action": action, "target_node": target, "rationale": d.get("rationale") or ""}


def make_llm_decide(orch: Orchestrator, cfg: HarnessConfig, *, mcp_servers=None,
                    template=None):
    """엔진 decide_fn 팩토리. 결정마다 fresh ORCHESTRATOR instance를 spawn한다."""
    from .workflow import DEFAULT_TEMPLATE
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
        except (DecisionError, Exception) as e:
            retry = await adapter.send(
                sid, f"직전 결정이 거부됐다({e}). allowed_actions "
                     f"{ALLOWED_BY_TRIGGER[trigger]} 중에서만 골라 다시 제출해.")
            try:
                return validate_decision(retry.structured, trigger, tmpl)
            except Exception as e2:
                return {"action": "ASK_USER", "target_node": None,
                        "rationale": f"invalid decision after retry: {e2}"}
    return decide
```

(`except (DecisionError, Exception)`은 예시 표기 — 실제로는 `WorkflowError`를 `DecisionError`로 감싼 뒤 `except DecisionError` 하나로 처리한다. validate_decision 내부에서 `template.node(target)`를 try/except WorkflowError로 감싸 DecisionError로 변환하라.)

spawn이 ORCHESTRATOR 번들을 로드하도록 `orchestrator.py`의 번들 로딩 조건을 `WORKER_ROLES | {Role.ORCHESTRATOR}`로 바꾸고, `start_worker`에 `mcp_servers` 키워드를 추가해 adapter로 관통시킨다(어댑터 변경은 Task 6 — 이 Task에서는 FakeAdapter에만 `mcp_servers` 수용을 추가하고 last_mcp_servers로 기록).

`DecisionEvent`는 엔진(Task 4)이 기록하므로 여기서는 기록하지 않는다 — 이중 기록 금지.

- [ ] **Step 4: 통과 확인** — `uv run pytest -q` 전체 green
- [ ] **Step 5: Commit** — `git add src/devcrew/decision.py src/devcrew/orchestrator.py src/devcrew/adapters/base.py tests && git commit -m "feat: LLM decision sessions with validation and fail-closed degradation"`

---

### Task 6: harness MCP server + ROLE_POLICY + adapter 관통

**Files:**
- Create: `src/devcrew/harness_mcp.py`
- Modify: `src/devcrew/enforcement.py` (ORCHESTRATOR policy)
- Modify: `src/devcrew/adapters/claude_code.py` (`mcp_servers` 수용·resume 캐시 포함)
- Modify: `src/devcrew/adapters/codex.py` (`mcp_servers` 전달 시 명시적 거부)
- Test: `tests/test_harness_mcp.py`

**Interfaces:**
- Consumes: `TraceStore.events/get_instance`, T4 엔진의 상태 뷰
- Produces: `build_harness_mcp(trace, engine=None) -> McpSdkServer` (server name `"harness"`), tool 3종: `get_execution_state`, `get_worker_result`, `get_trace_events`. `ROLE_POLICY[Role.ORCHESTRATOR].allowed_tools == ["mcp__harness__get_execution_state", "mcp__harness__get_worker_result", "mcp__harness__get_trace_events"]`

- [ ] **Step 1: SDK 시그니처 검증** — 구현 전에 실행해 tool 데코레이터·서버 생성 시그니처를 확정한다 (T7 방식):

```bash
uv run python -c "
from claude_agent_sdk import create_sdk_mcp_server, tool
import inspect
print(inspect.signature(create_sdk_mcp_server))
print(inspect.signature(tool))
print(inspect.getsource(tool))" | head -40
```

- [ ] **Step 2: 실패 테스트** — `tests/test_harness_mcp.py`. tool 핸들러를 직접 호출해 반환 데이터를 검증한다 (SDK 서버 기동 없이):

```python
import json
import pytest
from devcrew.harness_mcp import build_harness_mcp, _handlers  # _handlers: {name: async fn} 테스트 후크


@pytest.mark.asyncio
async def test_get_trace_events_filters(trace_with_events):
    h = _handlers(trace_with_events)
    out = await h["get_trace_events"]({"execution_id": "E1",
                                       "event_type": "WorkerResultEvent", "limit": 5})
    data = json.loads(out["content"][0]["text"])
    assert all(e["event_type"] == "WorkerResultEvent" for e in data)


@pytest.mark.asyncio
async def test_get_worker_result_returns_structured(trace_with_events):
    # WorkerResultEvent payload["structured"]가 그대로 반환됨 (Task 4에서 넣은 전문)
    ...


@pytest.mark.asyncio
async def test_get_execution_state_unknown_execution(trace_with_events):
    out = await h["get_execution_state"]({"execution_id": "NOPE"})
    assert "not found" in out["content"][0]["text"]


def test_orchestrator_policy_is_mcp_only():
    from devcrew.enforcement import ROLE_POLICY
    from devcrew.schema import Role
    assert ROLE_POLICY[Role.ORCHESTRATOR].allowed_tools == [
        "mcp__harness__get_execution_state", "mcp__harness__get_worker_result",
        "mcp__harness__get_trace_events"]
    assert not ROLE_POLICY[Role.ORCHESTRATOR].scoped_write_tools
```

- [ ] **Step 3: 구현**
  - `harness_mcp.py`: trace 조회로 3 tool 구현. `get_execution_state`는 `NodeTransitionEvent`/`DecisionEvent`를 모아 `{nodes 최근 전이, decisions, last_event_at}`을 합성 (엔진 인스턴스가 주어지면 live 상태 우선). 모든 tool은 읽기 전용 — TraceStore.append 호출 금지. 반환은 MCP content 규약 `{"content": [{"type": "text", "text": json.dumps(...)}]}`.
  - `enforcement.py`: `Role.ORCHESTRATOR`의 `RolePolicy(allowed_tools=[...3종...])` — exact-match이므로 기존 `_match`가 그대로 동작.
  - `claude_code.py`: `start_session(..., mcp_servers: dict | None = None)` 추가 → `ClaudeAgentOptions(mcp_servers=mcp_servers or {})`, `_start_opts` 캐시에 포함해 resume 시 재주입.
  - `codex.py`: `mcp_servers`가 truthy로 오면 `ValueError("codex adapter does not support harness mcp_servers")` — 조용한 무시 금지 (Orchestrator는 Claude 고정, §5.1).
  - `base.py` Protocol 시그니처 갱신.
- [ ] **Step 4: 통과 확인** — `uv run pytest -q` 전체 green
- [ ] **Step 5: Commit** — `git add -A src tests && git commit -m "feat: harness MCP read-only tools + orchestrator role policy + adapter passthrough"`

---

### Task 7: p09 live 스모크

**Files:**
- Create: `poc/p09_engine.py`

**Interfaces:**
- Consumes: 전체 스택 (engine + decision + MCP + 실 어댑터). `poc/_common.py`의 `record/stores` 재사용

- [ ] **Step 1: 스크립트 작성** — p08의 toy repo 패턴 재사용. 2-노드 템플릿(develop→review, loop_back)으로 실행하되 **첫 리뷰가 NOT_PASS를 내도록 결함 주입**: develop 노드 메시지를 "calc.py에 mul(a, b)를 추가하되 일부러 `return a + b`로 잘못 구현하고 보고해"로, review가 NOT_PASS→엔진 loop-back→dev 수정→review PASS 경로를 태운다. 추가로 CLASSIFY 결정 1회를 실제 LLM으로: DEFAULT_TEMPLATE 사용, decide_fn=`make_llm_decide(orch, cfg, mcp_servers={"harness": build_harness_mcp(trace)})`. 단 HIGH_CAPABILITY tier는 비용이 크므로 **p09 실행 시 cfg를 dataclasses.replace로 ORCHESTRATOR tier=CHEAP로 낮춘 사본**을 쓴다 (live는 CHEAP/CODEX_DEFAULT 원칙).

체크 항목 (`record("p09", checks, ...)`):
  - `completed`: ExecutionResult.status == "COMPLETED"
  - `loop_happened`: node_history에 review NOT_PASS 전이 존재
  - `fix_landed`: toy repo의 calc.py에 올바른 `mul` 구현 존재 (`mul(3,4)==12` subprocess 확인)
  - `classify_decision_recorded`: DecisionEvent 존재, action ∈ {PROCEED, SKIP_NODE}
  - `attribution`: Reviewer WorkerResultEvent의 instance_id가 `rev` prefix
  - `tokens_recorded`: ExecutionResult.total_tokens > 0
- [ ] **Step 2: 1회 실행** — `uv run python poc/p09_engine.py` → `poc/_artifacts/p09.json` 확인. **한 번만 실행** (LLM 호출 ~6회). 실패 시 원인 수정 후 재실행.
- [ ] **Step 3: Commit** — `git add poc/p09_engine.py && git commit -m "feat: p09 engine live smoke — review loop + classify decision"`

---

## 최종 검증·리뷰 (SDD controller 수행, implementer task 아님)

1. `uv run pytest -q` 전체 green + `uv run python poc/p09_engine.py` 결과 확인 (재실행 불필요 — Task 7 아티팩트 사용)
2. Codex 최종 리뷰: `codex exec -m gpt-5.6-sol -c model_reasoning_effort=high -s read-only --skip-git-repo-check -o <output>` — 수정 루프 최대 3회 (사용자 지시)
3. PASS 시 사용자 통지 (테스트 방법 포함)
