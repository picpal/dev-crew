# Agent Role 구성 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Worker 4종(Explorer/Developer/Reviewer/QA)의 role 번들(한국어 지침 + 출력 JSON Schema)과 §17 설정 로딩을 구현하고 spawn 주입 배관을 연결한다.

**Architecture:** `roles/<role>/{prompt.md, output.schema.json}` 번들을 `devcrew.roles`가 로드, `config/harness.yaml`을 `devcrew.config`가 로드해 routing이 소비. `Orchestrator.spawn`이 번들을 어댑터의 확장 파라미터(`system_prompt`, `output_schema`)로 전달. 최종 응답은 provider 네이티브 구조화 출력으로 스키마 강제되어 `TurnOutcome.structured`로 회수.

**Tech Stack:** Python 3.12 / uv / pytest / pyyaml / claude-agent-sdk(`output_format`·`system_prompt`·`ResultMessage.structured_output` — 로컬 0.2.139에서 검증됨) / openai-codex(`base_instructions`·`run(output_schema=)` — 0.144.4 검증됨)

**Spec:** `docs/superpowers/specs/2026-08-17-agent-roles-design.md` (같은 브랜치). 상위: DESIGN.md §3.3·§3.4·§5.2·§10.1·§17.

## Global Constraints

- 작업 worktree: `.worktrees-sdd/agent-roles` (branch feat/agent-roles, base d4f52ca). 모든 명령은 이 안에서.
- 셋업: `uv sync` 후 `uv run pytest -q` → **45 passed** 기준선 확인, `uv add pyyaml`
- 공통 status enum: `PASS | NOT_PASS | NEED_REPLAN | BLOCKED | INSUFFICIENT_CAPABILITY` — 모든 스키마·코드에서 이 표기 그대로
- 번들·yaml 누락은 fail-fast (RoleBundleError / ConfigError) — 코드 폴백·조용한 기본값 금지
- 프롬프트 원문을 AgentInstance/trace에 싣지 않는다 (`role_bundle_version`만)
- model ID pin·effort 5단계·기존 enforcement 로직 불변. `src/devcrew/{enforcement,worktree}.py`와 `store/`는 이번 effort에서 수정 금지 (adapters/base·claude_code·codex, orchestrator, routing은 수정 대상)
- live 호출은 CHEAP(`claude-sonnet-5`@low) / CODEX_DEFAULT(`gpt-5.6-terra`@medium)만, p08 총 4회
- 커밋 메시지는 conventional commits

---

## File Structure

```text
config/harness.yaml               # T1
src/devcrew/config.py             # T1
roles/{explorer,developer,reviewer,qa}/output.schema.json   # T2
src/devcrew/roles.py              # T2
roles/{explorer,developer,reviewer,qa}/prompt.md            # T3
src/devcrew/adapters/base.py      # T4 수정 (TurnOutcome.structured, FakeAdapter)
src/devcrew/adapters/claude_code.py  # T4 수정
src/devcrew/adapters/codex.py     # T4 수정
src/devcrew/routing.py            # T1 수정 (yaml 소비)
src/devcrew/schema.py             # T5 수정 (role_bundle_version 필드)
src/devcrew/orchestrator.py       # T5 수정 (번들 로드·주입)
poc/p08_roles.py                  # T6
tests/test_config.py, test_roles.py  # T1, T2
tests/(기존 파일 갱신)             # T4, T5
```

---

### Task 1: config 로딩 — harness.yaml이 tier·roleDefaults의 정본

**Files:**
- Create: `config/harness.yaml`, `src/devcrew/config.py`
- Modify: `src/devcrew/routing.py` (TIERS 하드코딩 제거)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `config.load(path=None) -> HarnessConfig(tiers: dict[str, Tier], role_defaults: dict[Role, RoleDefault])`, `RoleDefault(tier: str, effort: EffortLevel | None)`, `ConfigError`. `routing.TIERS`는 모듈 로드 시 `config.load()`로 채워짐 (기존 시그니처 유지 — `resolve(tier_name, effort)` 불변).

- [ ] **Step 1: 셋업 및 기준선**

```bash
cd /Users/picpal/Desktop/workspace/dev-crew/.worktrees-sdd/agent-roles
uv sync && uv run pytest -q   # 45 passed 확인
uv add pyyaml
```

- [ ] **Step 2: 실패하는 테스트 작성**

`tests/test_config.py`:

```python
import pytest
from devcrew.config import ConfigError, load
from devcrew.schema import EffortLevel, Role


def test_load_default_config():
    cfg = load()   # config/harness.yaml
    assert cfg.tiers["CHEAP"].model == "claude-sonnet-5"
    assert cfg.tiers["CODEX_HIGH_REASONING"].model == "gpt-5.6-sol"
    assert cfg.role_defaults[Role.EXPLORER].tier == "CHEAP"
    assert cfg.role_defaults[Role.REVIEWER].tier == "CODEX_DEFAULT"


def test_missing_config_is_fail_fast(tmp_path):
    with pytest.raises(ConfigError):
        load(tmp_path / "nope.yaml")


def test_invalid_effort_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "tiers:\n  CHEAP: {provider: CLAUDE_CODE, model: claude-sonnet-5, effort: TURBO}\n"
        "roleDefaults: {}\n")
    with pytest.raises(ConfigError):
        load(bad)


def test_routing_consumes_yaml():
    from devcrew.routing import TIERS, resolve
    assert TIERS["DEFAULT"].model == "claude-sonnet-5"
    assert resolve("CHEAP").effort == "low"
```

- [ ] **Step 3: 실패 확인**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: devcrew.config`

- [ ] **Step 4: 구현**

`config/harness.yaml`:

```yaml
# §17 — tier = (full model ID, 기본 effort) 쌍. alias 금지 (#12)
tiers:
  CHEAP:                { provider: CLAUDE_CODE, model: claude-sonnet-5, effort: LOW }
  DEFAULT:              { provider: CLAUDE_CODE, model: claude-sonnet-5, effort: HIGH }
  HIGH_CAPABILITY:      { provider: CLAUDE_CODE, model: claude-opus-5,   effort: HIGH }
  CODEX_DEFAULT:        { provider: CODEX,       model: gpt-5.6-terra,   effort: MEDIUM }
  CODEX_HIGH_REASONING: { provider: CODEX,       model: gpt-5.6-sol,     effort: HIGH }

roleDefaults:
  EXPLORER:  { tier: CHEAP }
  ARCHITECT: { tier: DEFAULT }
  DEVELOPER: { tier: DEFAULT }
  SECURITY:  { tier: DEFAULT, effort: HIGH }
  QA:        { tier: CHEAP }
  REVIEWER:  { tier: CODEX_DEFAULT }
```

`src/devcrew/config.py`:

```python
"""§17 설정 로딩 — 설정이 정본, 코드 폴백 없음."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .schema import EffortLevel, Provider, Role

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "harness.yaml"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class TierSpec:
    provider: Provider
    model: str
    default_effort: EffortLevel


@dataclass(frozen=True)
class RoleDefault:
    tier: str
    effort: EffortLevel | None = None


@dataclass(frozen=True)
class HarnessConfig:
    tiers: dict[str, TierSpec]
    role_defaults: dict[Role, RoleDefault]


def load(path: str | Path | None = None) -> HarnessConfig:
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        raise ConfigError(f"config not found: {p}")
    raw = yaml.safe_load(p.read_text())
    try:
        tiers = {
            name: TierSpec(Provider(t["provider"]), t["model"], EffortLevel(t["effort"]))
            for name, t in (raw.get("tiers") or {}).items()
        }
        role_defaults = {
            Role(name): RoleDefault(d["tier"],
                                    EffortLevel(d["effort"]) if d.get("effort") else None)
            for name, d in (raw.get("roleDefaults") or {}).items()
        }
    except (KeyError, ValueError) as e:
        raise ConfigError(f"invalid config {p}: {e}") from e
    if not tiers:
        raise ConfigError(f"config has no tiers: {p}")
    return HarnessConfig(tiers=tiers, role_defaults=role_defaults)
```

`src/devcrew/routing.py` 수정 — 하드코딩 TIERS 블록(`TIERS: dict[str, Tier] = {...}`)을 다음으로 교체 (Tier dataclass·resolve·validate_combo·SUPPORT_MATRIX·_EFFORT_STR은 불변):

```python
from .config import load as _load_config

_CFG = _load_config()
TIERS: dict[str, Tier] = {
    name: Tier(spec.provider, spec.model, spec.default_effort)
    for name, spec in _CFG.tiers.items()
}
ROLE_DEFAULTS = _CFG.role_defaults
```

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest tests/test_config.py tests/test_routing.py -v`
Expected: PASS (기존 routing 테스트도 yaml 경유로 green)

- [ ] **Step 6: 전체 회귀 + 커밋**

```bash
uv run pytest -q   # 49+ passed
git add config/ src/devcrew/config.py src/devcrew/routing.py tests/test_config.py pyproject.toml uv.lock
git commit -m "feat: harness.yaml config loading as source of truth for tiers"
```

---

### Task 2: RoleBundle 로더 + 출력 스키마 4종

**Files:**
- Create: `src/devcrew/roles.py`, `roles/<role>/output.schema.json` ×4
- Test: `tests/test_roles.py`

**Interfaces:**
- Produces: `RoleBundle(role: Role, prompt: str, schema: dict, version: str)`, `load_bundle(role: Role) -> RoleBundle`, `RoleBundleError`. version은 `sha256(prompt+schema)[:12]`.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_roles.py`:

```python
import pytest
from devcrew.roles import RoleBundleError, load_bundle
from devcrew.schema import Role

WORKERS = [Role.EXPLORER, Role.DEVELOPER, Role.REVIEWER, Role.QA]
STATUS_ENUM = ["PASS", "NOT_PASS", "NEED_REPLAN", "BLOCKED", "INSUFFICIENT_CAPABILITY"]


@pytest.mark.parametrize("role", WORKERS)
def test_bundle_loads_with_common_skeleton(role):
    b = load_bundle(role)
    assert len(b.prompt) > 200            # 실제 지침이 있어야 함
    props = b.schema["properties"]
    assert props["status"]["enum"] == STATUS_ENUM
    assert "summary" in props
    assert set(b.schema["required"]) >= {"status", "summary"}
    assert len(b.version) == 12


def test_role_specific_fields():
    assert "findings" in load_bundle(Role.EXPLORER).schema["properties"]
    assert "changed_files" in load_bundle(Role.DEVELOPER).schema["properties"]
    assert "verdict" in load_bundle(Role.REVIEWER).schema["properties"]
    assert "results" in load_bundle(Role.QA).schema["properties"]


def test_missing_bundle_fail_fast():
    with pytest.raises(RoleBundleError):
        load_bundle(Role.ORCHESTRATOR)    # 이번 effort 범위 밖 — 번들 없음
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_roles.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 스키마 4종 작성**

공통 골격(모든 파일 동일 부분): `type: object`, `additionalProperties: false`, `required`에 최소 `["status", "summary"]`, `status.enum`은 Global Constraints의 5값 그대로.

`roles/explorer/output.schema.json`:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["status", "summary", "findings", "affected_files"],
  "properties": {
    "status": {"type": "string", "enum": ["PASS", "NOT_PASS", "NEED_REPLAN", "BLOCKED", "INSUFFICIENT_CAPABILITY"]},
    "summary": {"type": "string"},
    "findings": {"type": "array", "items": {
      "type": "object", "additionalProperties": false,
      "required": ["file", "evidence", "confidence"],
      "properties": {
        "file": {"type": "string"},
        "line": {"type": ["integer", "null"]},
        "evidence": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1}
      }}},
    "affected_files": {"type": "array", "items": {"type": "string"}}
  }
}
```

`roles/developer/output.schema.json`:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["status", "summary", "changed_files", "build", "tests"],
  "properties": {
    "status": {"type": "string", "enum": ["PASS", "NOT_PASS", "NEED_REPLAN", "BLOCKED", "INSUFFICIENT_CAPABILITY"]},
    "summary": {"type": "string"},
    "changed_files": {"type": "array", "items": {"type": "string"}},
    "build": {"type": "object", "additionalProperties": false,
      "required": ["ok"], "properties": {"ok": {"type": "boolean"}, "detail": {"type": "string"}}},
    "tests": {"type": "object", "additionalProperties": false,
      "required": ["passed", "failed"],
      "properties": {"passed": {"type": "integer"}, "failed": {"type": "integer"}, "detail": {"type": "string"}}}
  }
}
```

`roles/reviewer/output.schema.json`:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["status", "summary", "verdict", "findings"],
  "properties": {
    "status": {"type": "string", "enum": ["PASS", "NOT_PASS", "NEED_REPLAN", "BLOCKED", "INSUFFICIENT_CAPABILITY"]},
    "summary": {"type": "string"},
    "verdict": {"type": "string", "enum": ["PASS", "NOT_PASS"]},
    "findings": {"type": "array", "items": {
      "type": "object", "additionalProperties": false,
      "required": ["severity", "description"],
      "properties": {
        "severity": {"type": "string", "enum": ["critical", "important", "minor"]},
        "file": {"type": ["string", "null"]},
        "line": {"type": ["integer", "null"]},
        "description": {"type": "string"}
      }}}
  }
}
```

`roles/qa/output.schema.json`:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["status", "summary", "plan", "results"],
  "properties": {
    "status": {"type": "string", "enum": ["PASS", "NOT_PASS", "NEED_REPLAN", "BLOCKED", "INSUFFICIENT_CAPABILITY"]},
    "summary": {"type": "string"},
    "plan": {"type": "array", "items": {"type": "string"}},
    "results": {"type": "array", "items": {
      "type": "object", "additionalProperties": false,
      "required": ["criterion", "ok"],
      "properties": {
        "criterion": {"type": "string"},
        "ok": {"type": "boolean"},
        "repro": {"type": ["string", "null"]}
      }}}
  }
}
```

- [ ] **Step 4: 로더 구현**

`src/devcrew/roles.py`:

```python
"""Role 번들 로더 — 지침(prompt.md) + 출력 계약(output.schema.json).

Adapter는 role을 모른다: spawn이 번들을 로드해 전달만 한다 (spec 결정 2).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .schema import Role

ROLES_DIR = Path(__file__).resolve().parents[2] / "roles"
_STATUS_ENUM = ["PASS", "NOT_PASS", "NEED_REPLAN", "BLOCKED", "INSUFFICIENT_CAPABILITY"]


class RoleBundleError(Exception):
    pass


@dataclass(frozen=True)
class RoleBundle:
    role: Role
    prompt: str
    schema: dict
    version: str


def load_bundle(role: Role, roles_dir: str | Path | None = None) -> RoleBundle:
    base = Path(roles_dir) if roles_dir else ROLES_DIR
    d = base / role.value.lower()
    prompt_p, schema_p = d / "prompt.md", d / "output.schema.json"
    if not prompt_p.exists() or not schema_p.exists():
        raise RoleBundleError(f"role bundle incomplete for {role.value}: {d}")
    prompt = prompt_p.read_text()
    schema = json.loads(schema_p.read_text())
    _validate_skeleton(role, schema)
    version = hashlib.sha256((prompt + json.dumps(schema, sort_keys=True)).encode()).hexdigest()[:12]
    return RoleBundle(role=role, prompt=prompt, schema=schema, version=version)


def _validate_skeleton(role: Role, schema: dict) -> None:
    props = schema.get("properties") or {}
    status = props.get("status") or {}
    if status.get("enum") != _STATUS_ENUM:
        raise RoleBundleError(f"{role.value}: status enum mismatch")
    if "summary" not in props or "status" not in (schema.get("required") or []):
        raise RoleBundleError(f"{role.value}: common skeleton missing")
```

Step 1 테스트의 `len(b.prompt) > 200`을 위해 T3 전까지는 각 `roles/<role>/prompt.md`에 placeholder가 아닌 **실제 초안**이 필요하다 — T2에서는 4개 파일을 T3의 확정본으로 함께 작성한다 (T3 내용 참조). T2와 T3는 같은 구현자가 연속으로 수행한다.

- [ ] **Step 5: 통과 확인 + 커밋**

Run: `uv run pytest tests/test_roles.py -v` → PASS

```bash
git add src/devcrew/roles.py roles/ tests/test_roles.py
git commit -m "feat: role bundle loader with output schemas for 4 worker roles"
```

---

### Task 3: role 지침 prompt.md 4종

**Files:**
- Create: `roles/{explorer,developer,reviewer,qa}/prompt.md` (T2에서 함께 커밋했다면 이 태스크는 내용 검수·보강)

**Interfaces:**
- Produces: 4절 고정 구성(책임/금지/작업 방식/보고 규칙)의 한국어 지침. §5.2·§3.4 내용과 일치해야 한다.

- [ ] **Step 1: 4개 파일 작성** — 아래 내용 그대로 (각 파일 4절 구성 동일 패턴).

`roles/explorer/prompt.md`:

```markdown
# EXPLORER

너는 dev-crew harness의 Explorer다. 코드베이스의 사실을 조사해 근거와 함께 보고한다.

## 책임
- 코드 위치, 호출 흐름, 의존성, 변경 영향도 조사
- 모든 주장에 file:line 근거를 붙인다. 추측은 confidence를 낮춰 명시한다
- 변경 영향을 받는 파일 목록(affected_files)을 빠짐없이 수집한다

## 금지
- 소스 파일 수정·생성 금지 (읽기 전용 role이다)
- 설계 제안·구현 판단 금지 — 사실만 보고한다
- 근거 없는 단정 금지

## 작업 방식
- Read/Grep/Glob과 git log·diff로 탐색한다
- 넓게 훑은 뒤 핵심 경로를 깊게 판다. 요청 범위 밖 탐색은 하지 않는다
- 확인 불가능한 것은 확인 불가로 보고한다 (BLOCKED가 아니라 findings의 낮은 confidence로)

## 보고 규칙
최종 응답은 스키마를 따른다:
- status: 조사 완료면 PASS, 요청이 모호해 재계획 필요면 NEED_REPLAN, 접근 불가 리소스면 BLOCKED
- findings[]: {file, line, evidence(한 줄 근거), confidence(0~1)}
- affected_files[]: 변경 시 영향받는 파일 경로
- summary: 두세 문장 요약
```

`roles/developer/prompt.md`:

```markdown
# DEVELOPER

너는 dev-crew harness의 Developer다. 할당된 task scope 안에서 구현하고 검증 결과를 보고한다.

## 책임
- 할당된 scope의 파일만 수정한다
- 구현 후 반드시 빌드·테스트를 실행하고 결과를 보고한다
- 변경한 모든 파일을 changed_files에 기록한다

## 금지
- task scope 밖 파일 수정 금지 (worktree 밖 쓰기는 harness가 차단한다)
- 테스트를 통과시키기 위한 assertion 약화·삭제 금지
- 실패를 감춘 보고 금지 — 실패는 실패로 보고한다

## 작업 방식
- 기존 코드 패턴을 먼저 읽고 따른다
- 작게 구현하고 자주 검증한다
- 같은 접근이 2회 실패하면 다른 접근을 시도하고, 그래도 막히면 NEED_REPLAN으로 보고한다

## 보고 규칙
- status: 구현·검증 완료 PASS / 검증 실패 NOT_PASS / 요구 불명확·설계 문제 NEED_REPLAN / 능력 한계 INSUFFICIENT_CAPABILITY / 외부 차단 BLOCKED
- changed_files[]: 수정·생성 파일 경로 전부
- build: {ok, detail} / tests: {passed, failed, detail}
- summary: 무엇을 왜 바꿨는지 두세 문장
```

`roles/reviewer/prompt.md`:

```markdown
# REVIEWER

너는 dev-crew harness의 Reviewer다. diff와 요구사항을 독립적으로 검토해 판정한다.

## 책임
- 구현 누락, 결함, task scope 위반, 테스트 적절성을 확인한다
- 모든 finding에 severity(critical/important/minor)와 위치를 붙인다
- 판정은 diff에 있는 증거로만 한다

## 금지
- 코드 수정 금지 — 판정과 지적만 한다 (읽기 전용이다)
- 스타일 취향 지적을 critical/important로 올리지 않는다
- 이전 리뷰 결과에 정박하지 않는다 — 매번 독립 판단한다

## 작업 방식
- 요구사항 → diff 순서로 읽는다 (diff부터 읽고 요구를 끼워맞추지 않는다)
- critical/important가 하나라도 있으면 verdict는 NOT_PASS
- 확신 없는 지적은 minor로 내리고 근거를 남긴다

## 보고 규칙
- status: 검토를 마쳤으면 PASS (검토 수행 자체의 성공), 검토 불가면 BLOCKED
- verdict: 코드에 대한 판정 PASS / NOT_PASS — status와 혼동하지 말 것
- findings[]: {severity, file, line, description}
- summary: 판정 근거 두세 문장
```

`roles/qa/prompt.md`:

```markdown
# QA

너는 dev-crew harness의 QA다. acceptance criteria 기반으로 테스트 계획을 세우고 실행해 검증한다.

## 책임
- 주어진 acceptance criteria를 검증 가능한 테스트 항목(plan)으로 분해한다
- 각 항목을 실제로 실행해 결과를 기록한다
- 실패 항목에는 재현 방법(repro)을 반드시 남긴다

## 금지
- 기능 설계 변경·구현 수정 금지
- 실행하지 않은 항목을 통과로 보고 금지
- criteria에 없는 임의 기준 추가 금지 (발견한 우려는 summary에 적는다)

## 작업 방식
- criteria당 최소 1개 검증 항목을 만든다
- 자동 실행 가능한 것은 명령으로 실행하고 출력을 근거로 삼는다
- 실행 불가 항목은 ok=false + repro에 사유를 남긴다

## 보고 규칙
- status: 전 항목 통과 PASS / 하나라도 실패 NOT_PASS / criteria 불명확 NEED_REPLAN / 실행 환경 차단 BLOCKED
- plan[]: 테스트 항목 문장
- results[]: {criterion, ok, repro}
- summary: 판정 요약 두세 문장
```

- [ ] **Step 2: 검증 + 커밋**

Run: `uv run pytest tests/test_roles.py -v` → PASS (프롬프트 길이 조건 포함)

```bash
git add roles/
git commit -m "feat: korean role prompts for 4 worker roles"
```

(T2에서 이미 함께 커밋했다면 이 태스크는 diff 없음 확인으로 종료)

---

### Task 4: 어댑터 확장 — structured 출력 회수

**Files:**
- Modify: `src/devcrew/adapters/base.py`, `claude_code.py`, `codex.py`
- Test: `tests/test_fake_adapter.py` (케이스 추가)

**Interfaces:**
- Produces: `TurnOutcome.structured: dict | None` (기본 None). `start_session(inst, initial_message, *, system_prompt: str | None = None, output_schema: dict | None = None)` — 세 어댑터(Fake 포함) 동일 시그니처. FakeAdapter는 `structured_script: list[dict] | None`을 받아 turn마다 반환.

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_fake_adapter.py`에 추가:

```python
async def test_fake_adapter_structured_output():
    fa = FakeAdapter(structured_script=[{"status": "PASS", "summary": "ok"}])
    sid = await fa.start_session(make_inst(), "go",
                                 system_prompt="지침", output_schema={"type": "object"})
    out = await fa.send(sid, "final")
    assert out.structured == {"status": "PASS", "summary": "ok"}
    assert fa.last_system_prompt == "지침"          # 주입 확인용 기록
    assert fa.last_output_schema == {"type": "object"}
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_fake_adapter.py -v`
Expected: FAIL — unexpected keyword / attribute

- [ ] **Step 3: base.py 수정**

`TurnOutcome`에 `structured: dict | None = None` 필드 추가. `FakeAdapter.__init__`에 `structured_script: list[dict] | None = None` 추가, `last_system_prompt`/`last_output_schema` 속성 초기화(None). `start_session` 시그니처 확장해 두 값을 기록. `send`에서 `structured_script`가 있으면 turn 인덱스에 맞춰 `TurnOutcome(..., structured=...)` 반환(스크립트 소진 시 None).

- [ ] **Step 4: claude_code.py 수정**

`start_session(self, inst, initial_message, *, system_prompt=None, output_schema=None)`:

```python
options = ClaudeAgentOptions(
    model=inst.model,
    effort=inst.effort_level.value.lower(),
    can_use_tool=make_can_use_tool(inst.role, self.trace, task_id=inst.execution_id,
                                   workspace_root=inst.worktree),
    system_prompt=system_prompt,
    output_format={"type": "json_schema", "schema": output_schema} if output_schema else None,
    **kw,
)
```

`_turn`에서 `ResultMessage.structured_output`을 회수해 `TurnOutcome(structured=getattr(result_msg, "structured_output", None), ...)`.

- [ ] **Step 5: codex.py 수정**

`start_session(..., *, system_prompt=None, output_schema=None)`: `thread_start(base_instructions=system_prompt, ...)`, `self._schemas[thread.id] = output_schema` 저장. `send`에서 `thread.run(message, effort=..., output_schema=self._schemas.get(session_id))`, 응답 파싱:

```python
structured = None
if self._schemas.get(session_id) and result.final_response:
    try:
        structured = json.loads(result.final_response)
    except json.JSONDecodeError:
        structured = None    # provider가 스키마 강제하므로 정상 경로에선 발생 안 함
```

resume()의 세션 설정 복원 dict(T-fixwave에서 추가된 `_session_cfg`)에 schema도 포함해 재적용.

- [ ] **Step 6: 통과 확인 + 전체 회귀 + 커밋**

```bash
uv run pytest -q     # 전체 green
git add src/devcrew/adapters/ tests/test_fake_adapter.py
git commit -m "feat: structured output plumbing through all adapters"
```

---

### Task 5: spawn 통합 — 번들 로드·주입·기록

**Files:**
- Modify: `src/devcrew/schema.py` (`role_bundle_version: str | None = None` 필드), `src/devcrew/orchestrator.py`
- Test: `tests/test_orchestrator.py` (케이스 추가)

**Interfaces:**
- Produces: `Orchestrator.spawn(...)`은 기존 시그니처 유지. 내부에서 `roles.load_bundle(role)`을 시도해 성공 시 `inst.role_bundle_version` 기록. 번들이 없는 role(ORCHESTRATOR/ARCHITECT/SECURITY)은 **번들 없이 spawn 허용하되** `bundle=None` (이번 effort 범위 밖 role의 기존 POC 경로 보존 — fail-fast는 번들이 "있어야 하는" WORKER_ROLES 집합에만 적용). 신규: `Orchestrator.start_worker(inst, initial_message) -> str` — 번들을 로드해 어댑터 `start_session`에 `system_prompt`/`output_schema`로 전달하는 헬퍼.

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_orchestrator.py`에 추가:

```python
from devcrew.roles import load_bundle

async def test_spawn_records_bundle_version(tmp_path):
    orch, trace, reg = make_orch(tmp_path)
    inst = await orch.spawn(Role.EXPLORER, "CHEAP", execution_id="E1",
                            node_id="n1", task_scope="*")
    assert inst.role_bundle_version == load_bundle(Role.EXPLORER).version


async def test_start_worker_injects_bundle(tmp_path):
    from devcrew.adapters.base import FakeAdapter
    fake = FakeAdapter(structured_script=[{"status": "PASS", "summary": "ok"}])
    orch, _, _ = make_orch(tmp_path, fake)
    inst = await orch.spawn(Role.QA, "CHEAP", execution_id="E1",
                            node_id="n1", task_scope="*")
    await orch.start_worker(inst, "검증 시작")
    assert fake.last_system_prompt.startswith("# QA")
    assert fake.last_output_schema["properties"]["results"]


async def test_spawn_without_bundle_for_out_of_scope_role(tmp_path):
    orch, _, _ = make_orch(tmp_path)
    inst = await orch.spawn(Role.ARCHITECT, "DEFAULT", execution_id="E1",
                            node_id="n1", task_scope="*")
    assert inst.role_bundle_version is None
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_orchestrator.py -v` → FAIL

- [ ] **Step 3: 구현**

`schema.py`: `AgentInstance`에 `role_bundle_version: str | None = None` (다른 Optional 필드들 옆에).

`orchestrator.py`:

```python
from .roles import RoleBundle, RoleBundleError, load_bundle

WORKER_ROLES = {Role.EXPLORER, Role.DEVELOPER, Role.REVIEWER, Role.QA}

# spawn() 내부, inst 생성 후 trace 기록 전:
bundle: RoleBundle | None = None
if role in WORKER_ROLES:
    bundle = load_bundle(role)          # 누락 시 RoleBundleError 전파 (fail-fast)
    inst.role_bundle_version = bundle.version

# 신규 메서드:
async def start_worker(self, inst: AgentInstance, initial_message: str) -> str:
    bundle = load_bundle(inst.role)     # WORKER_ROLES 전제; 아니면 RoleBundleError
    adapter = self.adapters[inst.provider]
    session_id = await adapter.start_session(
        inst, initial_message,
        system_prompt=bundle.prompt, output_schema=bundle.schema)
    inst.session_id = session_id
    self.registry.upsert(inst, provider_ref=None)
    return session_id
```

- [ ] **Step 4: 통과 확인 + 전체 회귀 + 커밋**

```bash
uv run pytest -q     # 전체 green (기존 spawn 테스트는 WORKER_ROLES라 번들 로드됨 — DEVELOPER 사용 테스트들이 자연 통과하는지 확인)
git add src/devcrew/schema.py src/devcrew/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: spawn loads role bundle and start_worker injects it"
```

---

### Task 6: p08 live 스모크 + 실행

**Files:**
- Create: `poc/p08_roles.py`

- [ ] **Step 1: 스크립트 작성**

```python
"""p08 — role 번들 live 스모크: 4 worker role이 스키마 적합 출력을 내는가.

각 role CHEAP/CODEX_DEFAULT 1회, 장난감 repo 대상. 총 4 LLM 호출.
"""
import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

from _common import record, stores

STATUS_ENUM = {"PASS", "NOT_PASS", "NEED_REPLAN", "BLOCKED", "INSUFFICIENT_CAPABILITY"}


def toy_repo() -> Path:
    repo = Path(tempfile.mkdtemp(prefix="p08-"))
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=p@p", "-c", "user.name=p",
                    "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


async def main():
    from devcrew.adapters.claude_code import ClaudeCodeAdapter
    from devcrew.adapters.codex import CodexAdapter
    from devcrew.orchestrator import Orchestrator
    from devcrew.schema import Provider, Role

    trace, registry = stores()
    claude, codex = ClaudeCodeAdapter(trace, registry), CodexAdapter(trace, registry)
    orch = Orchestrator(trace, registry,
                        {Provider.CLAUDE_CODE: claude, Provider.CODEX: codex})
    repo = toy_repo()
    checks: dict[str, bool] = {}
    outputs: dict[str, dict] = {}

    async def run_role(name, role, tier, prompt):
        inst = await orch.spawn(role, tier, execution_id="POC-8",
                                node_id=name, task_scope="*", worktree=str(repo))
        sid = await orch.start_worker(inst, prompt)
        adapter = orch.adapters[inst.provider]
        out = await adapter.send(sid, "이제 최종 보고를 스키마대로 제출해.")
        s = out.structured
        outputs[name] = s or {}
        checks[f"{name}_structured"] = isinstance(s, dict)
        checks[f"{name}_status_valid"] = isinstance(s, dict) and s.get("status") in STATUS_ENUM
        await adapter.archive(sid)
        registry.finish(inst.instance_id)

    await run_role("explorer", Role.EXPLORER, "CHEAP",
                   "calc.py에서 sub 함수의 위치와 add와의 차이를 조사해 보고해.")
    await run_role("developer", Role.DEVELOPER, "CHEAP",
                   "calc.py에 mul(a, b) 함수를 추가하고 python -c로 동작 확인 후 보고해.")
    await run_role("reviewer", Role.REVIEWER, "CODEX_DEFAULT",
                   "git diff HEAD와 calc.py를 읽고 mul 추가가 적절한지 검토 판정해.")
    await run_role("qa", Role.QA, "CHEAP",
                   "acceptance criteria: 'mul(3,4)==12'. 검증 계획을 세우고 실행해 보고해.")

    checks["explorer_has_findings"] = bool(outputs["explorer"].get("findings"))
    checks["developer_changed_files"] = "calc.py" in " ".join(
        outputs["developer"].get("changed_files", []))
    checks["reviewer_verdict_present"] = outputs["reviewer"].get("verdict") in {"PASS", "NOT_PASS"}
    checks["qa_results_present"] = bool(outputs["qa"].get("results"))

    record("p08", checks, extra={"outputs": outputs})


asyncio.run(main())
```

- [ ] **Step 2: 컴파일 확인 + 커밋**

```bash
uv run python -m py_compile poc/p08_roles.py && uv run pytest -q
git add poc/p08_roles.py
git commit -m "poc: role bundle live smoke script (p08)"
```

- [ ] **Step 3: live 실행 (컨트롤러 수행)**

Run: `uv run python poc/p08_roles.py`
Expected: 12개 체크 전부 ✅, `poc/_artifacts/p08.json`. 실패 시 outputs의 실제 구조를 보고 스키마/프롬프트/배관 중 어디가 원인인지 진단 후 fix 루프.

---

## 최종 리뷰 (사용자 지시 반영)

- **Codex로 수행**: `codex exec` (모델 `gpt-5.6-sol`, `-c model_reasoning_effort=high`, read-only sandbox)에 브랜치 diff(d4f52ca..HEAD)와 spec을 주고 리뷰. 결과를 findings로 정리.
- **수정 루프 최대 3회** — 3회 후 잔여는 adjudicate·기록.
- PASS 후 사용자 테스트 안내와 함께 통지.

## Self-Review 체크 결과

- Spec coverage: 결정 1(스키마)→T2, 결정 2(배관)→T4·T5, 결정 3(trace)→T5, 결정 4(파싱)→T4, 결정 5(fail-fast)→T1·T2·T5, 결정 6(프롬프트)→T3, 검증 기준→T1~T5 unit + T6 live. 누락 없음.
- Placeholder 스캔: 없음 — 프롬프트·스키마 전문 포함.
- Type consistency: `start_session(inst, msg, *, system_prompt, output_schema)` T4 정의 = T5 `start_worker` 호출 = FakeAdapter 시그니처 일치. `RoleBundle.version`(12 hex) = `role_bundle_version` 기록 일치. STATUS_ENUM 5값이 T2 스키마·T6 스크립트에서 동일.
- 알려진 조정 지점: Claude `output_format`의 정확한 dict 형태(로컬 0.2.139 docstring 기준 — T4 Step 4에서 실차 확인), Codex `base_instructions`가 시스템 프롬프트 역할을 완전히 하는지(p08에서 관측).
