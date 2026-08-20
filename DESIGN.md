# dev-crew — Multi-Agent Engineering Harness Design

> Status: Source of Truth  
> Project: dev-crew  
> Version: 0.5.0  
> Updated: 2026-08-15  
> Decisions: [Wayfinder Map #1](https://github.com/picpal/dev-crew/issues/1) — 결정의 정본은 각 티켓의 resolution 코멘트, 이 문서는 결과만 반영  
> Scope: Slack 기반 사용자 요청을 Claude Code 및 Codex Agent로 분해·실행·검증하고, 실행 결과를 GitHub와 외부 HTML Report로 제공하는 로컬 우선 Engineering Harness

## 1. 문서 목적

이 문서는 `dev-crew` Multi-Agent Engineering Harness의 기준 설계다. 구현, POC, 운영 정책, 평가 기준은 이 문서를 우선한다.

`dev-crew`는 프로젝트와 제품의 공식 명칭이다. `Harness`는 문서 안에서 `dev-crew`를 구성하는 실행·조정 시스템을 가리키는 기술적 구성요소명으로 사용한다.

핵심 목표는 단일 Claude Code 세션에서 사용자가 탐색, 설계, 개발, 검토, 재작업을 계속 지휘하던 방식을 다음 구조로 전환하는 것이다.

```text
User
  ↓
Slack
  ↓
Harness
  ↓
Orchestrator
  ↓
Dynamic Stateful Workflow
  ↓
Explorer / Architect / Developer / Security / Reviewer / QA
  ↓
GitHub Issue·PR + External HTML Report URL
```

Harness는 LLM 계산량을 무조건 최소화하는 시스템이 아니다. 필요한 역할 분리와 검증에 계산량을 사용하되, 사람의 개입, 완료 시간, 재작업, 누락을 줄이고 그 효과를 측정하는 시스템이다.

## 2. 설계 목표와 비목표

### 2.1 목표

- 사용자는 Orchestrator라는 단일 창구를 통해 요청, 질문, 추가 지시, 승인을 처리한다.
- Orchestrator는 업무를 분류하고 필요한 Agent Role과 Workflow를 동적으로 선택한다.
- 독립적인 개발 작업은 별도 Developer Instance와 Worktree로 병렬 처리한다.
- 개발 결과는 Local Review와 필요한 경우 Integration Review를 거친다.
- Review, QA, Security 실패를 조건부 루프로 처리하고 무한 반복을 방지한다.
- Role별 provider 기본값을 유지하면서 작업의 complexity, risk, role에 따라 model과 reasoning effort를 선택한다.
- 모든 실행을 Task 중심 trace로 기록해 AS-IS와 TO-BE를 비교한다.
- GitHub Issue는 사람 친화적인 영구 Audit Trail로 유지한다.
- Slack에는 HTML 자체가 아니라 외부에서 열 수 있는 Report URL을 전달한다.
- 정적 HTML Report는 전체 사이트 rebuild 없이 Task 완료 직후 업로드하고 조회할 수 있어야 한다.

### 2.2 비목표

- 처음부터 완전 자유형 Agent mesh를 구현하지 않는다.
- Worker Agent가 사용자와 직접 대화하지 않는다.
- Orchestrator가 코드 탐색, 구현, 상세 리뷰를 직접 수행하지 않는다.
- 모든 Task에 모든 역할을 투입하지 않는다.
- 모든 작업을 최고 비용 모델로 시작하지 않는다.
- Raw trace 전체를 Slack이나 GitHub Issue 본문에 복사하지 않는다.
- GitHub Pages를 Report Hosting Layer로 사용하지 않는다.
- MVP에서 대규모 대시보드나 복잡한 프론트엔드 애플리케이션을 만들지 않는다.

## 3. 핵심 설계 원칙

### 3.1 Orchestrator는 Control Plane이다

Orchestrator는 다음을 담당한다.

- 사용자 요청 해석과 상태 응답
- Task complexity와 risk 분류
- Workflow 생성과 변경
- Agent Role, instance 수, provider, model, reasoning effort 결정
- 작업 할당, 의존성, 병렬화, join 관리
- loop, retry, invalidation, escalation 관리
- Agent 결과 취합과 사용자 커뮤니케이션

Orchestrator는 repository를 직접 탐색하거나 코드를 수정하지 않는다. 새로운 전문적 사실이 필요하면 해당 Worker에게 위임한다. 이미 확보된 Task Knowledge를 이용한 판단, 취합, 상태 관리는 직접 수행한다.

### 3.2 Hub-and-Spoke를 기본 통신 구조로 사용한다

```text
                    User
                     │
                  Slack
                     │
                Orchestrator
        ┌────────────┼────────────┐
        ↓            ↓            ↓
     Explorer     Developer     Reviewer
        ↓            ↓            ↓
     Architect    Security        QA
```

- 논리적 Hub는 Orchestrator다.
- Message Transport는 판단하지 않고 지정된 session에 메시지만 전달한다.
- Worker 간 직접 mesh 통신은 기본 금지한다.
- Worker 간 의존 정보는 Orchestrator가 Task Knowledge 또는 artifact로 전달한다.

### 3.3 Role과 Agent Instance를 분리한다

Role은 공유되는 직무 정의와 capability policy다. Agent Instance는 특정 Task를 수행하기 위해 생성된 독립 실행 단위다.

```text
Role: DEVELOPER
  ├─ DEV-001 / Session A / Worktree A
  ├─ DEV-002 / Session B / Worktree B
  └─ DEV-003 / Session C / Worktree C
```

같은 Role의 Instance는 동일한 역할 정책과 프로젝트 규칙을 사용하지만, session context, task scope, worktree, model assignment, lifecycle은 독립적이다.

### 3.4 Capability는 prompt가 아니라 Harness가 강제한다

역할 분리는 System Prompt와 프로젝트 지침에만 의존하지 않는다. Tool allowlist, permission, hook으로 실행 경계를 강제한다.

| Role | 핵심 capability | 기본 금지 |
|---|---|---|
| Orchestrator | orchestration, messaging, workflow, state, session management | repository read/write, build, test |
| Explorer | filesystem read, grep, git read, dependency tracing | source write |
| Architect | artifact read, design, trade-off analysis | source write |
| Developer | filesystem read/write, build, unit test | 사용자 직접 통신 |
| Security | filesystem read, security analysis/tools | 승인 없는 source write |
| Reviewer | filesystem read, diff, review, verification | feature implementation |
| QA | build, test, runtime verification | 기능 설계 변경 |

강제 메커니즘은 harness가 소유한 선언적 role policy를 Adapter가 spawn 시점에 provider 네이티브 수단으로 변환한다([#13](https://github.com/picpal/dev-crew/issues/13)).

- Claude Code 계열 role: `allowed_tools`/`disallowed_tools` allowlist + harness 소유 `can_use_tool` 콜백(allowlist 밖 호출 거부 및 trace 기록) + `cwd`·경로 스코프 permission rule로 worktree 격리
- Codex Reviewer: OS 수준 `Sandbox.read_only` + `cwd`=검토 대상 worktree
- Orchestrator: repo tool을 일절 부여하지 않고 harness의 orchestration MCP tool만 노출
- 감사는 hook이 아니라 SDK 메시지 스트림의 tool_use 블록을 ToolCallEvent로 수집 — 경계(콜백)와 관측(스트림)을 분리

### 3.5 정적 템플릿으로 시작하고 동적 그래프로 확장한다

MVP는 승인된 Workflow Template을 사용하되, Orchestrator가 역할 생략, 병렬 branch, loop, escalation을 정책 범위 내에서 결정한다. 장기적으로 Stateful Workflow Graph로 확장한다.

## 4. 전체 아키텍처

```text
┌──────────────────────────────────────────────────────────────────┐
│                           Slack                                  │
│  Request / Question / Approval / Cancel / Summary + Links        │
└───────────────────────────────┬──────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────┐
│                         Local Harness                            │
│                                                                  │
│  Slack Adapter                                                   │
│      ↓                                                           │
│  Task Service ── GitHub Adapter ── Issue / PR                    │
│      ↓                                                           │
│  Orchestrator                                                    │
│      ├─ Task Classifier                                          │
│      ├─ Workflow Planner / Stateful Loop Controller              │
│      ├─ Model Routing Policy                                     │
│      ├─ Task Knowledge                                           │
│      └─ Context Lifecycle Manager                                │
│      ↓                                                           │
│  Dispatcher / Scheduler                                          │
│      ├─ Concurrency Policy                                       │
│      ├─ Review Queue                                             │
│      └─ Message Transport                                        │
│      ↓                                                           │
│  Agent Runtime                                                   │
│      ├─ Claude Code Adapter / Provider                           │
│      ├─ Codex Adapter / Provider                                 │
│      ├─ Session Registry                                         │
│      └─ Worktree Manager                                         │
│                                                                  │
│  Trace / Metrics Store                                           │
│      ├─ Raw append-only events                                   │
│      └─ Task / Agent / Model aggregates                          │
│                                                                  │
│  Report Service                                                  │
│      ├─ Report Generator                                         │
│      ├─ HTML Renderer                                            │
│      └─ Hosting Adapter / Cloudflare R2 Publisher                │
└───────────────────────────────┬──────────────────────────────────┘
                                │ upload static HTML
                         ┌──────▼──────┐
                         │ Cloudflare R2│
                         └──────┬──────┘
                                │ object fetch
                         ┌──────▼──────────┐
                         │Cloudflare Worker│
                         │/tasks/{taskId}  │
                         └──────┬──────────┘
                                │ external Report URL
                         ┌──────▼──────┐
                         │ Browser/User │
                         └─────────────┘
```

## 5. 역할과 기본 Provider 정책

### 5.1 기본 매핑

| Role | 기본 adapter/provider | 목적 |
|---|---|---|
| Orchestrator | Claude Code | 사용자 대화, workflow 및 state 관리 |
| Explorer | Claude Code | 코드 사실, 호출 경로, 영향도 조사 |
| Architect | Claude Code | 설계, trade-off, contract 판단 |
| Developer | Claude Code | 구현, build, unit test |
| Security | Claude Code | 위협, 취약점, 보안 검증 |
| QA | Claude Code | 테스트 계획 및 실행 검증 |
| Reviewer | **Codex** | Local Review와 Integration Review |

Reviewer는 Codex adapter/provider를 기본으로 한다. 나머지 Orchestrator, Explorer, Architect, Developer, Security, QA는 Claude Code adapter/provider를 기본으로 한다.

Provider 기본값은 역할별 독립성과 평가 일관성을 위한 정책이다. 장애 대응이나 실험을 위한 fallback은 별도 POC와 승인된 policy가 있을 때만 허용한다. Provider override가 발생하면 반드시 routing reason을 기록한다.

### 5.2 역할별 기본 동작

#### Orchestrator

- 사용자 요청과 추가 지시의 단일 진입점
- 직접 repository tool을 사용하지 않음
- 작업 분해, 역할 선택, 모델 라우팅, 상태 전이 수행
- Agent 결과의 근거와 신선도를 확인해 응답

#### Explorer

- 코드 위치, 호출 흐름, 의존성, 변경 영향도 조사
- 결과를 file/line, evidence, confidence, affected files 형태로 반환
- 변경된 파일과 연결된 Task Knowledge를 stale 처리할 근거 제공

#### Architect

- 요구사항을 contract, component, data flow, failure mode로 구체화
- 복잡도, 통합 위험, 보안 위험을 갱신
- Developer가 실행할 수 있는 artifact를 생산

#### Developer

- 할당된 task scope만 수정
- 독립적인 session과 worktree 사용
- build와 unit test 결과 및 changed files를 반환
- 독립 구현 단위가 여러 개면 다수 Instance로 확장

#### Security

- 고위험 설계나 구현의 threat와 finding을 검증
- 수정이 필요한 finding은 invalidation 범위를 함께 반환

#### Reviewer

- Codex provider를 사용해 diff와 요구사항을 독립 검토
- Local Review는 각 Developer 결과마다 수행
- Integration Review는 shared contract나 dependency가 있는 병렬 결과의 join 이후 수행
- Reviewer queue pressure에 따라 Instance를 확장

#### QA

- acceptance criteria 기반 테스트 계획과 실행 결과 검증
- 실패 시 재현 정보와 영향 범위를 반환
- 코드 변경 후 무효화된 review/security/QA 단계를 재실행하도록 신호

## 6. AgentInstance와 Provider Adapter

### 6.1 AgentInstance 스키마

```yaml
AgentInstance:
  instanceId: string
  role: ORCHESTRATOR | EXPLORER | ARCHITECT | DEVELOPER | SECURITY | REVIEWER | QA

  provider: CLAUDE_CODE | CODEX
  adapter: string
  model: string
  effortLevel: LOW | MEDIUM | HIGH | XHIGH | MAX
  reasoningLevel: LOW | MEDIUM | HIGH | XHIGH | MAX
  routingPolicyVersion: string
  routingReason: string

  sessionId: string
  parentInstanceId: string | null
  replacedInstanceId: string | null
  escalationChainId: string | null

  executionId: string
  workflowId: string
  nodeId: string
  taskScope: string
  worktree: string | null

  skills: [string]
  tools: [string]
  permissions: [string]

  status: CREATED | RUNNING | WAITING | DONE | FAILED | BLOCKED | ARCHIVED
  createdAt: timestamp
  startedAt: timestamp | null
  completedAt: timestamp | null
```

`effortLevel`은 provider 공통 정책 표현이다. 5단계는 양 provider의 실제 옵션에 1:1 대응한다([#12](https://github.com/picpal/dev-crew/issues/12) — 조사 결과 양쪽 모두 5단계이며 4단계 정규화는 정보 손실). Adapter는 이를 provider가 지원하는 실제 reasoning/effort 옵션으로 변환한다. `reasoningLevel`은 실제 요청에 적용된 정규화 값으로 기록해 provider 간 비교에 사용한다. 지원하지 않는 조합은 Adapter가 조용히 변경하지 않고 routing validation error로 반환해야 한다 — 단 Claude Code는 미지원 effort를 조용히 하향하는 것이 기본 동작이므로, Adapter가 소유한 정적 지원 매트릭스(model × effort)로 **호출 전에** 자체 검증해야 한다. `ultracode`(Claude Code)와 `ultra`(Codex)는 model effort가 아니라 provider 오케스트레이션 모드이므로 `effortLevel`에 넣지 않으며, harness가 오케스트레이션을 담당하는 MVP에서는 두 모드 모두 사용하지 않는다.

### 6.2 Adapter 계약

```text
startSession(agentInstance, initialMessage) -> sessionId
send(sessionId, message) -> eventStream
resume(sessionId, message) -> eventStream
cancel(sessionId) -> status
archive(sessionId) -> status
getUsage(sessionId) -> token/latency/provider metadata
```

Claude Code와 Codex의 실제 session/thread semantics는 다를 수 있다. Harness는 공통 lifecycle을 제공하되 provider 고유 ID와 raw usage metadata를 보존한다.

구동 표면은 양 provider 모두 공식 Python SDK다([#11](https://github.com/picpal/dev-crew/issues/11), 조사: [session-surfaces](./docs/research/session-surfaces.md)).

- Claude Code Adapter: `claude-agent-sdk`의 `ClaudeSDKClient`(streaming 모드 — `interrupt()`가 이 경로에서만 지원되어 cancel을 위해 강제됨). `archive`는 네이티브 부재로 합성 구현: harness 상태(`ARCHIVED`) + `tag_session()` + transcript 파일 보관. transcript는 내부 포맷이므로 이동·보관만 하고 파싱 금지.
- Codex Adapter: `openai-codex`의 `AsyncCodex` — 6개 연산 전부 네이티브. harness당 인스턴스 1개를 모든 Reviewer instance가 공유하며 `thread_id`가 `sessionId`가 된다. app-server 직접 호출은 비권장(불안정 표면).
- CLI(`claude -p`, `codex exec`)는 디버깅·재현용 폴백으로만 유지한다. Codex의 `steer()`는 계약에 추가하지 않는다(Phase 2+ 후보).
- 토큰 회계는 `usage`가 아니라 `model_usage` 기준(subagent 포함 범위가 필드별로 다름). `total_cost_usd`는 client-side 추정치로 청구 근거 사용 금지. usage 스키마는 양 provider 합집합 + raw payload 보존.

### 6.3 Session 복구

- Harness 재시작 후 Session Registry에서 active instance를 복구한다. Registry는 `harness.db`의 활성-only 테이블이다([#15](https://github.com/picpal/dev-crew/issues/15)): 살아있는 행(CREATED/RUNNING/WAITING/BLOCKED)만 보관하고 종결 시 제거하며, 전체 이력은 trace의 projection이 담당한다. Orchestrator 자신의 세션도 같은 registry의 한 행이다.
- 복구는 **lazy verify**다: 재시작 시 행마다 전제조건만 검증(Claude: transcript 파일 존재, Codex: `thread_list` 조회)하고 RESUMABLE로 표시한다. 실제 resume은 다음 dispatch 때 수행한다. 재시작 시 LLM 호출은 0회다.
- 검증 실패 또는 dispatch 시 resume 실패면 기존 instance를 `FAILED_RECOVERY`로 종료하고, 요약 artifact를 전달한 새 instance를 생성한다. transcript가 기본 30일 보존·로컬 한정이므로 이 경로는 예외가 아니라 **필수 경로**다.
- 인계 artifact는 7.6절 escalation 인계 스키마를 재사용한다(reason=`FAILED_RECOVERY`). 이를 위해 매 turn 완료 시 Orchestrator가 resultSummary와 artifact 포인터를 registry 행에 갱신하는 **turn 체크포인트**를 규칙화한다. worktree는 16.3절대로 보존되어 새 instance가 인수한다.
- recovery 때문에 model 또는 provider가 변경되면 별도 event와 reason을 기록한다.

## 7. Model Routing Policy

### 7.1 책임과 입력

Agent spawn 시 Orchestrator는 Role만 정하지 않는다. Model Routing Policy를 사용해 다음을 함께 결정한다.

```text
Spawn Decision
  = role
  + provider
  + model
  + effort/reasoning level
  + instance count
  + task scope
```

정책 입력은 최소한 다음을 포함한다.

- `role`: 수행 책임과 요구 capability
- `complexity`: 변경 범위와 추론 난이도
- `risk`: 실패 영향과 검증 필요성
- `evidence`: 파일 수, module 수, dependency, ambiguity, failure history
- `attemptHistory`: 이전 Agent Instance 결과와 동일 finding 반복 여부
- `budget`: Task와 loop의 token/time 한도

### 7.2 Complexity 분류

| 등급 | 기준 예시 |
|---|---|
| LOW | 1~2개 파일, 기존 패턴 반복, 국소 수정, 명확한 acceptance criteria |
| MEDIUM | 여러 파일, 일부 dependency 영향, 제한된 설계 판단, module 내부 contract 변경 |
| HIGH | cross-module 변경, architecture 변경, 복잡한 migration, 불명확한 요구사항, 큰 통합 범위 |

### 7.3 Risk 분류

| 등급 | 기준 예시 |
|---|---|
| LOW | 실패 영향이 작고 rollback과 자동 검증이 쉬움 |
| MEDIUM | 사용자 동작, 데이터 contract, 여러 소비자에 영향 |
| HIGH | payment, authentication, authorization, security, privacy, data loss, concurrency, irreversible migration |

### 7.4 Role × Complexity × Risk 선택 Rubric

모델 이름은 환경과 provider 정책에 따라 달라지므로 문서에는 capability tier를 정의하고, 실제 model ID는 설정으로 매핑한다.

| Role/상황 | 시작 tier | effort/reasoning | 정책 |
|---|---|---|---|
| Orchestrator, 일반 분류·상태 관리 | DEFAULT | MEDIUM | context를 작게 유지하고 Worker 근거 사용 |
| Explorer, 국소 검색 | CHEAP | LOW~MEDIUM | 빠른 모델 우선, 근거 부족 시 승격 |
| Explorer, cross-module 영향도 | DEFAULT | MEDIUM~HIGH | dependency와 history 분석 강화 |
| Architect, LOW/MEDIUM risk | DEFAULT | MEDIUM | 표준 설계 판단 |
| Architect, HIGH complexity/risk | HIGH_CAPABILITY | HIGH | architecture/security/concurrency 중심 |
| Developer, 국소 패턴 수정 | CHEAP 또는 DEFAULT | MEDIUM | cheap/default-first |
| Developer, 일반 기능 개발 | DEFAULT | MEDIUM~HIGH | build/test feedback 사용 |
| Developer, HIGH complexity/risk | HIGH_CAPABILITY | HIGH | 병렬화보다 일관성이 중요한지 함께 판단 |
| Security, 단순 checklist | DEFAULT | MEDIUM | Claude Code provider 유지 |
| Security, 고위험 threat 분석 | HIGH_CAPABILITY | HIGH | 독립 검증과 evidence 요구 |
| QA, 단순 로그·테스트 확인 | CHEAP | LOW~MEDIUM | 자동 결과 요약 중심 |
| QA, 복합 failure 분석 | DEFAULT 또는 HIGH_CAPABILITY | HIGH | 재현과 원인 분리 |
| Reviewer, Local Review | CODEX_DEFAULT | MEDIUM | Codex provider 고정 |
| Reviewer, Integration Review (low/medium risk) | CODEX_DEFAULT | MEDIUM | 항상 독립 새 instance ([#4](https://github.com/picpal/dev-crew/issues/4)) |
| Reviewer, Integration Review (high risk) | CODEX_HIGH_REASONING | HIGH | 항상 독립 새 instance, `xhigh` 승격 가능 |

동일 complexity에서도 risk가 높으면 최소 한 단계 상향한다. Role의 성격상 판단 부담이 크면 complexity가 낮아도 effort를 높일 수 있다. 모든 선택은 `routingPolicyVersion`과 `routingReason`으로 재현 가능해야 한다.

### 7.5 Cheap/default-first

모든 Agent를 최고 모델로 시작하지 않는다.

```text
Lowest sufficient tier
        ↓
실행 및 검증
        ↓
PASS ───────────────→ 다음 단계
        │
        └─ capability 부족 / 반복 실패 / risk 상승
                         ↓
                   MODEL_ESCALATION
```

첫 모델은 role, complexity, risk 기준에서 충분하다고 판단되는 가장 낮은 tier를 선택한다. 비용 절감 자체보다 성공 작업당 비용과 lead time, 품질을 함께 최적화한다.

### 7.6 MODEL_ESCALATION

다음 조건 중 하나가 발생하면 Orchestrator는 escalation을 검토한다.

- 같은 finding이 2~3회 반복되어 no-progress가 감지됨
- build/test/review 실패가 모델의 추론 또는 context 한계로 분류됨
- 실행 중 architecture, security, concurrency, data-loss risk가 상향됨
- Agent가 `NEED_REPLAN` 또는 `INSUFFICIENT_CAPABILITY`를 반환함
- 기존 tier의 time/token budget 대비 진척이 임계치 미만임

Escalation은 실행 중인 session의 model을 바꾸지 않는다. 관측 가능성과 책임 경계를 위해 **새 Agent Instance를 spawn**한다.

```text
DEV-001
provider=CLAUDE_CODE
model=DEFAULT
result=NEED_REPLAN
        │
        ├─ MODEL_ESCALATION event
        │    reason, evidence, previous usage
        ↓
DEV-002
provider=CLAUDE_CODE
model=HIGH_CAPABILITY
effort=HIGH
replacedInstanceId=DEV-001
```

새 instance에는 전체 raw context가 아니라 다음을 전달한다.

- 원래 task scope와 acceptance criteria
- 이전 instance의 요약과 artifacts
- 실패 evidence와 unresolved findings
- changed files 또는 worktree 상태
- escalation reason과 금지된 반복 전략

Provider 기본값은 escalation만으로 변경하지 않는다. Developer의 모델 승격은 Claude Code provider 안에서, Reviewer의 모델 승격은 Codex provider 안에서 수행한다. Provider fallback은 별도 장애 정책이다.

## 8. Dynamic Agent Scaling과 병렬 개발

### 8.1 Developer는 workload-driven scaling을 사용한다

Developer는 singleton이 아니라 Role이다. Orchestrator는 병렬화 가치가 있는 독립 작업 단위 수만큼 Developer Instance를 생성한다.

판단 기준은 다음과 같다.

1. 작업이 서로 독립적인가?
2. 동일 파일을 동시에 수정하지 않는가?
3. 선후 의존성이 없는가?
4. 별도 worktree로 격리 가능한가?
5. 병렬 이득이 merge/integration 비용보다 큰가?

```yaml
concurrencyPolicy:
  developer:
    minInstances: 0
    maxInstances: 3
    spawnPolicy: WORKLOAD_DRIVEN
```

```text
Parallel Group
  ├─ DEV-001 / API / Worktree A
  ├─ DEV-002 / Batch / Worktree B
  └─ DEV-003 / Admin / Worktree C
```

강하게 결합된 Controller-Service-Mapper 변경처럼 분리 비용이 더 크면 하나의 Developer Instance가 끝까지 담당한다.

### 8.2 Project 규칙과 작업 공간

각 Worktree는 동일 commit에서 생성되어 다음 프로젝트 규칙의 동일 버전을 사용한다.

```text
Project Worktree
  ├─ CLAUDE.md
  ├─ .claude/
  └─ source/
```

Role policy는 Harness가 관리하고, 프로젝트 지식과 규칙은 repository가 관리한다.

```text
Developer Role Policy
  + Project CLAUDE.md / .claude
  + Task Scope
  + Model Routing Decision
  → Developer Instance
```

### 8.3 Subagent 경계

- 독립 구현, 독립 retry, 독립 review, 별도 worktree가 필요하면 Developer Instance를 생성한다.
- 짧은 탐색, 테스트 원인 조사, 대안 비교처럼 부모 Developer의 책임 안에 머무는 보조 작업만 내부 Subagent로 처리한다.
- MVP는 Agent-level parallelism을 먼저 측정하고 In-Agent parallelism은 후속 단계로 둔다.

## 9. Reviewer Queue와 Review 구조

### 9.1 Reviewer는 queue-driven scaling을 사용한다

Developer 수와 Reviewer 수를 1:1로 고정하지 않는다.

```text
DEV-001 ─┐
DEV-002 ─┼──→ Review Queue ──→ REV-001
DEV-003 ─┘                    └→ REV-002 when pressure rises
```

```yaml
concurrencyPolicy:
  reviewer:
    minInstances: 1
    maxInstances: 3
    spawnPolicy: QUEUE_PRESSURE
    scaleSignals:
      - queueLength
      - oldestWaitTime
      - reviewerUtilization
```

Reviewer Instance는 모두 Codex adapter/provider를 사용한다. Queue pressure가 낮으면 일관성과 비용을 위해 기본 1개를 유지한다.

### 9.2 Local Review

- 각 Developer 결과마다 필수다.
- 구현 누락, 코드 품질, 테스트, task scope 준수를 확인한다.
- NOT_PASS면 해당 Developer branch만 재작업한다.
- 수정 후 기존 Local Review 결과를 무효화하고 다시 queue에 넣는다.

### 9.3 Integration Review

Local Review를 통과한 병렬 결과는 join 후 필요에 따라 Integration Review를 수행한다.

```text
DEV-A → Local Review A PASS ─┐
DEV-B → Local Review B PASS ─┼→ Join → Integration Review → QA
DEV-C → Local Review C PASS ─┘
```

Integration Review는 다음을 확인한다.

- branch 간 interface와 shared contract 일치
- transaction, exception, concurrency 경계
- 중복 또는 충돌 구현
- merge 후 전체 acceptance criteria
- 개별 테스트는 통과하지만 조합에서 실패하는 문제

Dependency와 shared contract가 없는 완전 독립 변경은 정책에 따라 Integration Review를 생략할 수 있다.

Integration Review의 실행 규칙은 다음과 같다([#4](https://github.com/picpal/dev-crew/issues/4)).

- **risk와 무관하게 항상 새 `REV-INTEGRATION-*` instance로 분리한다.** Local Review를 수행한 session은 자신이 승인한 branch에 정박하므로 이어받지 않는다. 분리는 항상, tier는 risk가 결정한다(low/medium은 CODEX_DEFAULT·MEDIUM, high는 CODEX_HIGH_REASONING·HIGH).
- 입력은 구조 artifact만 준다: join된 diff, task scope·acceptance criteria, shared contract, changed-files 맵. Local Review의 verdict·finding은 주지 않는다(anchoring 방지). 중복 지적은 독립 검증이 작동한다는 신호로 본다.
- NOT_PASS → 재작업 → 재검토 루프는 같은 instance를 resume한다. 같은 finding이 3회 반복되면 loop guard(10.3절)가 독립 Integration Reviewer escalation으로 새 instance를 spawn한다.

Integration Review 실패 시 모든 Developer를 재실행하지 않는다. Orchestrator는 영향받은 node만 invalidate하고 다시 Local Review와 Integration Review를 수행한다.

## 10. Stateful Workflow와 Loop Control

Workflow는 단순 DAG가 아니라 조건부 branch, parallel, join, loop, retry, escalation, invalidation을 포함하는 Stateful Workflow Graph다.

### 10.1 기본 상태

```text
PENDING
  ↓
RUNNING
  ├─ PASS ─────────────→ NEXT
  ├─ RETRYABLE_FAIL ───→ LOOP
  ├─ NEED_REPLAN ──────→ ORCHESTRATOR
  └─ BLOCKED ──────────→ HUMAN
```

### 10.2 대표 루프

| Loop | 조건 | 복귀 대상 | 기본 invalidation |
|---|---|---|---|
| Dev ↔ Review | Review NOT_PASS | 해당 Developer | Local Review |
| Dev ↔ QA | Test FAIL | 영향받은 Developer | Review, QA |
| Dev ↔ Security | Security finding | 영향받은 Developer | Review, Security, QA |
| Architect ↔ Security | 설계 보안 이슈 | Architect | Design Security |
| Explorer ↔ Orchestrator | 근거 부족 | Explorer | 관련 Task Knowledge |
| Workflow ↔ Orchestrator | 계획 오류 | Re-plan | 영향받은 downstream nodes |

### 10.3 Loop Guard

```yaml
loopPolicy:
  maxIterations: 5
  maxDurationMinutes: 60
  maxTokenBudget: 1500000          # 실행 전체 hard cap (무인 실행 최후 안전판)
  sameFindingEscalationThreshold: 3
  roleBudgets:                     # 에이전트별 토큰 "경보선" (종료 트리거 아님)
    ORCHESTRATOR: 400000
    DEVELOPER: 600000
    REVIEWER: 500000
    # …나머지 role
```

한도 초과는 Task의 즉시 실패가 아니라 자동 루프 종료와 Orchestrator escalation을 의미한다.

**토큰 예산의 위상 (2026-08-20 개정).** `adapter.send()` 한 번이 그 에이전트의 전체
agentic turn(내부 tool 루프 포함)이므로 토큰은 turn이 끝난 뒤에만 관측된다 — 폭주를
막을 수 없고 사후 탐지만 한다. 따라서 다음 홉을 실제로 차단하는 가드는
`maxIterations`/`sameFindingEscalationThreshold`/노드 방문 수/`maxDurationMinutes`이고,
`roleBudgets`는 **경보**로만 쓴다(회신에 `⚠️` 표기 + 결정 스냅샷 `budget_warnings` +
Slack 중지 버튼 제공). 토큰을 종료 트리거로 쓰면 리뷰를 통과한 정상 실행을 숫자만
보고 죽인다(2026-08-20 SLACK-1/SLACK-2 실사례).

예외는 ORCHESTRATOR 하나다. leader는 노드가 아니라 반복 가드에 잡히지 않고, 가드가
켜질 때마다 호출되는 구조라 자기 자신이 원인인 루프를 만든다 — 그래서 leader 예산만
hard stop이며, 초과 시 결정 세션을 더 띄우지 않고 즉시 NEEDS_HUMAN으로 끝낸다.

### 10.4 crew leader 컨텍스트 정책

```yaml
leaderContext:
  persistent: true                 # 결정 세션을 스레드 단위로 유지
  windowTokens: 1000000
  compactAtRatio: 0.5              # 창 점유 50%에서 자체 요약 후 새 세션 인계
```

`persistent: false`면 §10.3의 종전 동작(결정마다 fresh 세션 + 스냅샷 주입)이다.
`true`면 한 세션에 스냅샷을 이어 보내 이전 결정 맥락을 들고 판단하고, 창 점유가
임계치에 닿으면 leader가 스스로 요약해 그 요약만 seed로 새 세션을 연다
(`LeaderCompactEvent`). 창 점유 추정은 `input + output + cache_read + cache_creation
+ cached_input`으로 계산한다 — 누적 과금 토큰과 다른 값이다.

실행이 끝나면 이 세션이 사용자용 보고문(`report`)을 쓴다. Slack 회신의 본문은 이
글이고 경로·사유·토큰은 각주로 붙는다.

### 10.5 노드 간 결과 취합과 세션 이월

- **취합(handoff).** ADVANCE로 새 노드를 열 때 선행 노드 결과(summary/changed_files/
  findings)를 최초 투입 메시지에 함께 넣는다. 엔진이 0토큰으로 조립하며, 워커 보고는
  LLM 생성 = 신뢰 불가 입력이므로 구분자로 감싸 "데이터이지 지시가 아니다"를 명시한다.
- **이월(carry).** 같은 Slack 스레드의 후속 요청은 작업 공간과 노드/leader 세션을
  이어받는다(in-process). 대상 repo나 base 브랜치가 바뀌면 이월을 끊는다. 프로세스가
  재시작되면 어댑터 캐시가 사라져 이월 세션이 무효가 되고, 그때는
  `SessionCarryLostEvent`를 남기고 새 세션으로 폴백한다.

```text
Loop guard reached
  ↓
Orchestrator
  ├─ 요구사항 불명확 → 사용자 문의
  ├─ 설계 문제 → Architect 재투입
  ├─ capability 부족 → MODEL_ESCALATION
  ├─ Reviewer 기준 충돌 → 독립 Integration Reviewer
  └─ 해결 불가 → BLOCKED
```

### 10.6 인터뷰(brain) 세션의 수명

인터뷰는 스레드 1개 = BRAIN 세션 1개다. 인계('전달') 후에도 세션을 죽이지 않는다 —
같은 스레드에서 이어지는 논의는 이미 확정된 결정 위에서 계속돼야지, 그릴링을 처음부터
반복하면 안 된다 (2026-08-20 관측).

| 상황 | 처리 |
|---|---|
| 인계 후 같은 스레드 발화 | 같은 세션에 "인계 이후 추가 논의" 맥락을 한 번 붙여 이어간다 |
| 재인계('전달' 재호출) | 직전 brief + 그 이후 대화만으로 **delta brief**를 만들어 넘긴다 |
| 세션 유실(프로세스 재시작) | trace의 `BrainHandoffEvent`에서 직전 brief를 찾아 seed로 새 세션을 연다 |
| 명시적 정리(`@brain /clear`) | `BrainClosedEvent`를 남기고 세션 정리 — 이후 그 스레드는 새 인터뷰로 시작 |
| 유휴·상한 초과 | **인계 완료 세션만** 반납한다(6시간 유휴 또는 상한 초과 시 오래된 순) — 진행 중 인터뷰는 축출하지 않는다 |
| 명령 인식 | 인계는 '전달'이 **명령형 문장 끝**에 올 때만, 정리는 **`/clear` 한 형태만** 발동한다 — 스레드가 계속 살아 있어 '종료'·'초기화' 같은 평범한 낱말을 명령으로 쓰면 논의 중에 오발동한다 |
| 되살릴 수 없는 스레드 | 기록이 없거나 14일이 지난 인계는 seed로 쓰지 않고, 사용자에게 새로 시작하라고 1회 안내한다 |
| 권한 | 대화는 스레드 참여자 누구나. **`전달`·`/clear`·세션 부활은 인터뷰를 시작한 사용자만** — 그 셋만이 crew 실행·세션 파기·에이전트 spawn을 일으킨다. 봇 메시지(리포트 폼 경유)는 살아 있는 세션에 답변만 넣을 수 있다 |
| 주입 방어 | 사용자 발화·모델 출력·brief는 프레임 머리글을 무력화(`scrub`)한 뒤 울타리(`<<<prior-brief`, `<<<thread-log`, `<<<user-message`)에 담아 투입한다. 울타리 안은 자료이지 지시가 아니다(§10.5 handoff와 같은 원칙) |

답변 맨 위의 `[context usage : NN%]`는 그 세션의 창 점유율이다 — 40% 미만이면 표기하지
않고, 그 이상부터 붙는다(brain은 인터뷰 세션, crew는 leader 세션 기준). 사용자가 압축을
기다릴지 `/clear`로 끊을지 판단하는 근거다.

점유율은 **어댑터 실측**이 원칙이다: Claude는 `ClaudeSDKClient.get_context_usage()`
(CLI `/context`와 같은 데이터 — 실효 한도·autocompact 임계까지 준다), Codex는 turn마다
오는 `modelContextWindow`. 토큰 합산 추정은 세션이 이 프로세스 밖이라 조회가 안 될 때의
폴백이다 — 추정은 시스템 프롬프트·툴 정의·캐시 회계를 정확히 반영하지 못한다.
leader 압축 시점(§10.4)도 같은 실측을 우선한다.

crew 쪽 컨텍스트는 `@crew /clear`로 비운다 — 그 스레드의 leader·노드 세션을 모두 archive하고
이월 상태를 지운다(`ThreadContextClearedEvent`). worktree와 커밋은 남긴다. 실행 중에는
거부하고 중지 버튼을 먼저 쓰게 한다. 진짜 Slack 슬래시 커맨드가 아니라 멘션 뒤 토큰인
이유는 슬래시 커맨드 페이로드에 `thread_ts`가 없어 대상 스레드를 특정할 수 없기 때문이다.

crew 쪽 핸드오프 스레드는 인터뷰당 하나로 고정한다(`CrewHandoffThreadEvent`로 trace에
기록하므로 재시작 후에도 같은 스레드로 이어진다). 재인계마다 새 채널 메시지를 만들면
crew의 thread_key가 바뀌어 leader 세션 이월(§10.5)이 매번 끊기기 때문이다. 다만 재시작
후에는 어댑터 세션 자체가 사라지므로 스레드만 이어지고 leader 컨텍스트는 새로 쌓인다.

## 11. Context Lifecycle과 Task Knowledge

### 11.1 Context 전환 정책

Task 전환 시 Orchestrator는 session context를 다음 중 하나로 처리한다.

| Action | 사용 조건 |
|---|---|
| CONTINUE | 같은 module/issue의 직접 후속 작업이며 기존 context가 유효함 |
| COMPACT | 관련성은 높지만 raw history가 과도함 |
| ROTATE | 무관한 Task, stale context, 권한 또는 model 격리가 필요함 |

Context 결정은 `ContextEvent`로 기록한다. Model escalation은 기존 session을 CONTINUE하지 않고 새 instance를 생성한다.

### 11.2 Task Knowledge

Orchestrator는 Worker가 생산한 검증 가능한 사실을 Task Knowledge로 관리한다.

```yaml
TaskKnowledge:
  knowledgeId: string
  taskId: string
  fact: string
  sources: [string]
  producedByInstanceId: string
  producedAt: timestamp
  affectedFiles: [string]
  validity: CURRENT | STALE | INVALID
  confidence: number
```

Developer가 affected file을 수정하면 관련 사실을 `CURRENT → STALE`로 바꾼다. 사용자 질문에 필요한 최신 근거가 없을 때만 Explorer 또는 해당 전문 Role을 재호출한다.

## 12. Task, Dispatcher, 메시지 모델

### 12.1 Task lifecycle

```text
Slack Request
  ↓
Idempotency Check
  ↓
TaskExecution 생성
  ↓
GitHub Issue 생성: IN_PROGRESS
  ↓
Classify / Plan / Execute / Verify
  ↓
TaskEvaluation
  ↓
GitHub Issue 최종 갱신
  ↓
HTML Report 생성 및 Cloudflare publish
  ↓
Slack 완료 메시지
```

### 12.2 Dispatcher 책임

- Orchestrator가 결정한 node를 실행 가능한 Agent Instance에 전달
- provider별 동시성 제한과 rate limit 관리
- Developer workload queue와 Reviewer queue 분리
- 같은 session에 동시 메시지가 들어가지 않도록 serialize 또는 provider 정책 적용
- cancel, timeout, retry, recovery 신호 전달
- 업무 판단이나 Role 선택은 하지 않음

### 12.3 공통 메시지 envelope

```yaml
AgentMessage:
  messageId: string
  executionId: string
  workflowId: string
  nodeId: string
  fromInstanceId: string
  toInstanceId: string
  type: REQUEST | RESULT | QUESTION | FINDING | CONTROL
  payload: object
  artifacts: [string]
  correlationId: string
  createdAt: timestamp
```

Slack event는 provider event ID와 team/channel/thread ID를 이용해 중복 처리를 방지한다.

Slack 연결은 **Socket Mode**다([#8](https://github.com/picpal/dev-crew/issues/8)) — 로컬 우선 Harness에 공개 endpoint를 요구하지 않고 outbound WebSocket만 사용한다. 구현은 `slack-bolt` Python의 AsyncApp + AsyncSocketModeHandler. 토큰은 bot token(xoxb)과 app-level token(xapp, `connections:write`)을 환경 주입한다. Socket Mode는 ack 실패 시 재전송하므로 envelope의 event_id가 idempotency 키다. Events API 전환은 조직 배포 시의 Phase 3 재결정 사항이다.

## 13. Observability와 Evaluation Architecture

### 13.1 원칙

- 모든 기록은 최종적으로 `taskId`와 `executionId`로 연결한다.
- Raw event는 append-only로 보존하고 aggregate는 재계산 가능하게 한다.
- 저장 기술은 SQLite(WAL)다([#7](https://github.com/picpal/dev-crew/issues/7)). append-only `events` 테이블(event_type, ids, ts, raw payload JSON)이 유일한 진실이고, entity 테이블(13.3절 레코드)은 in-place 갱신되는 projection으로 events에서 재구축 가능해야 한다. 파일은 수명 주기로 분리한다: `trace.db`(events + projections, 성장·아카이브 대상) / `harness.db`(Session Registry 등 운영 상태).
- GitHub Issue와 HTML Report에는 사람이 볼 요약을 제공하고 raw trace는 Store에 둔다.
- AS-IS와 TO-BE에서 공통 관찰 단위를 동일하게 유지한다.
- Agent Role뿐 아니라 provider, model, effort/reasoning별 비용, 속도, 품질을 비교한다.

### 13.2 계층

```text
TaskExecution
  ├─ WorkflowExecution
  ├─ AgentInstance
  │    └─ AgentTurn
  │         ├─ ToolCallEvent
  │         ├─ HandoffEvent
  │         └─ UsageEvent
  ├─ ModelRoutingEvent
  ├─ ModelEscalationEvent
  ├─ LoopEvent
  ├─ ContextEvent
  ├─ HumanInteractionEvent
  ├─ ReportPublicationEvent
  └─ TaskEvaluation
```

### 13.3 핵심 레코드

#### TaskExecution

```text
executionId, taskId, benchmarkTaskId, mode
taskType, complexity, risk, repository
startedAt, completedAt, status
acceptanceCriteria
firstPassSuccess, reworkCount
humanInterventionCount, humanInterventionSeconds
totalInputTokens, totalOutputTokens, totalCachedTokens
totalAgentCalls, totalToolCalls, totalHandoffs
defectCount, escapedDefectCount
```

#### AgentTurn

```text
turnId, executionId, workflowId, nodeId
instanceId, agentRole, sessionId
provider, model, effortLevel, reasoningLevel
startedAt, completedAt, firstTokenAt
inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens
toolCallCount, handoffCount
resultStatus, resultSummary
```

#### ModelRoutingEvent

```text
eventId, executionId, instanceId
role, complexity, risk
provider, model, effortLevel, reasoningLevel
routingPolicyVersion, routingReason
candidateTiers, selectedTier
timestamp
```

#### ModelEscalationEvent

```text
eventId, executionId, escalationChainId
fromInstanceId, toInstanceId
fromProvider, fromModel, fromEffort
toProvider, toModel, toEffort
reason, evidence, iteration
tokensBeforeEscalation, latencyBeforeEscalation
timestamp
```

#### ReportPublicationEvent

```text
publicationId, executionId, taskId
rendererVersion, templateVersion
hostingProvider, bucket, objectKey
reportUrl, contentHash
uploadStartedAt, publishedAt
status, retryCount, errorCode
```

### 13.4 모델별 관측 지표

각 model assignment에 대해 다음을 집계한다.

| 분류 | Metric |
|---|---|
| Token/Cost | input/output/cache tokens, tokens per successful node/task |
| Latency | queue wait, time to first token, turn duration, end-to-end node latency |
| Quality | pass rate, first-pass rate, review findings, rework, escaped defect |
| Routing | selection frequency, escalation rate, escalation success, over/under-tier rate |
| Reliability | timeout, provider error, recovery, cancellation rate |

Provider나 model의 품질을 단일 점수로 단정하지 않는다. Role, complexity, risk cohort별로 비교한다.

### 13.5 최상위 KPI

| KPI | 기대 방향 |
|---|---:|
| Task Lead Time | 감소 |
| Human Touch Rate | 감소 |
| First-Pass Success Rate | 증가 |
| Rework Rate | 감소 |
| Tokens per Successful Task | 관리/최적화 |
| Defect Escape Rate | 감소 |

내부 KPI에는 Reviewer Queue Wait, Reviewer Utilization, Integration Failure Rate, First-Pass Review Rate, 평균 Loop 횟수, Model Escalation Rate, Escalation Success Rate, Orchestrator Direct-Code-Access Rate를 포함한다.

### 13.6 AS-IS / TO-BE 비교

```text
AS-IS: User → Claude Code Single Session
TO-BE: User → Slack → Multi-Agent Harness
```

동일한 `benchmarkTaskId`, acceptance criteria, 평가자를 사용해 pair로 비교한다. SMALL, MEDIUM, LARGE 및 risk cohort를 분리한다.

```text
Benchmark B-021
               AS-IS     TO-BE      Change
Lead Time       91m       48m        -47%
Human Touch      13         4        -69%
Tokens         210k      340k        +62%
Rework            2         0
First Pass        N         Y
Defects            3         1
```

TO-BE만 상세 로깅하고 AS-IS는 시간만 재는 비교는 금지한다. 공통 schema는 Execution, Turn, Tool, HumanInteraction, Evaluation이며, TO-BE에는 Workflow, Handoff, ContextTransition, AgentRole, ModelRouting을 확장한다.

## 14. GitHub Task Report / Audit Trail

GitHub Issue는 일감의 영구 기록, 검색, 사용자 검토, 후속 지시를 위한 Human-readable Audit Trail이다. PR은 실제 diff와 code review 기록을 담당한다. Trace Store는 machine-readable raw data를 담당한다.

```text
Raw Trace / Metrics
        ↓
Task Report Aggregate
        ├─ GitHub Issue: summary / audit / decisions
        └─ HTML Report: visual detail / workflow / drill-down
```

### 14.1 Lifecycle

1. Slack 요청 수신 시 Issue를 생성하고 `IN_PROGRESS`로 표시한다.
2. complexity, risk, selected workflow, benchmark ID를 기록한다.
3. Task 완료 시 같은 Issue의 본문 또는 최종 comment를 갱신한다.
4. PR이 있으면 Issue와 양방향 연결한다.
5. HTML Report URL을 Issue에도 기록한다.

### 14.2 권장 Issue 템플릿

```markdown
# Task Report

## 요청
- Task ID:
- Requested via: Slack
- Acceptance Criteria:

## 작업 분류
- Complexity:
- Risk:
- Workflow:
- Model Routing Summary:

## 수행 결과
- Explorer:
- Architect:
- Developer:
- Security:
- Reviewer:
- QA:

## 주요 의사결정
- Decision / Reason / Alternatives

## 변경 파일
- ...

## 검증
- Build:
- Test:
- Review:
- Security:

## 작업 지표
- Lead Time:
- Human Interventions:
- Tokens:
- Review Loops:
- Model Escalations:

## 링크
- Detailed HTML Report:
- Pull Request:

## 최종 결과
- Status:
- First Pass:
- Remaining Risk:
```

Issue에는 raw tool log나 전체 Agent 대화를 넣지 않는다.

## 15. External HTML Report와 Cloudflare Hosting

### 15.1 전달 원칙

Slack은 Notification과 Entry Point다. 상세 결과는 브라우저에서 여는 HTML Report다.

- Slack에 HTML 파일이나 HTML 본문을 직접 보내지 않는다.
- Slack에는 짧은 요약, Report URL, GitHub Issue 링크, PR 링크를 보낸다.
- Report URL은 사용자 환경의 localhost가 아니라 외부에서 접근 가능한 URL이어야 한다.

```text
✅ TASK-142 완료

Summary: 카드 목록 BIN 정보 추가
Review: PASS · QA: PASS
Lead Time: 38m · Review Loop: 1

Detailed Report: https://report.example.com/tasks/TASK-142
GitHub Issue: https://github.example/.../issues/142
Pull Request: https://github.example/.../pull/318
```

### 15.2 Hosting 결정

Report Hosting Layer는 **Cloudflare R2 + Cloudflare Worker**를 사용한다.

GitHub Pages는 구현이 단순하지만 push/build 이후 실제 조회까지 배포 지연이 발생한 경험 때문에 사용하지 않는다. 또한 Task마다 전체 정적 사이트 build를 수행하는 구조는 즉시성 요구와 맞지 않는다.

Cloudflare Pages 역시 Task별 full-site rebuild 경로로 사용하지 않는다. R2 object upload와 Worker serving을 사용해 Report 하나만 즉시 추가한다.

### 15.3 권장 흐름

```text
Task 완료
  ↓
Harness가 Trace / Metrics / Task artifacts 집계
  ↓
Report Generator
  ↓
HTML Renderer가 정적 HTML report 생성
  ↓
Cloudflare R2 Publisher가 object 업로드
  ↓
Cloudflare Worker가 /tasks/{taskId}로 서빙
  ↓
Slack에 요약 + Report URL + GitHub Issue/PR link 전달
```

핵심 데이터 흐름은 다음과 같다.

```text
Trace / Metrics Store
        ↓
Report Generator
        ↓
HTML Renderer
        ↓
Cloudflare R2 Publisher / Hosting Adapter
        ↓
Cloudflare R2
        ↓
Cloudflare Worker
        ↓
https://report.example.com/tasks/{taskId}
        ↓
Slack URL
```

이 경로에는 Git commit, Pages build, 전체 사이트 rebuild가 없다. Task별 정적 HTML object를 업로드한 직후 URL로 조회할 수 있다.

### 15.4 Report Service 컴포넌트

#### Report Service

- Task 완료 event를 수신해 report pipeline을 orchestration한다.
- 같은 execution에 대한 중복 publish를 idempotent하게 처리한다.
- 생성, 업로드, URL 발급 상태를 Task와 Publication Event에 기록한다.
- HTML publish가 실패해도 성공한 개발 결과를 잃지 않으며 재시도 가능하게 한다.

#### Report Generator

- Trace/Metrics Store와 Task artifact에서 report view model을 만든다.
- raw log를 그대로 노출하지 않고 역할별 요약과 근거를 구조화한다.
- Workflow, Developer parallel branches, review loop, model escalation, decision, changed files, QA/Security 결과를 포함한다.

#### HTML Renderer

- view model을 독립된 정적 HTML로 렌더링한다.
- template version과 renderer version을 기록한다.
- Report는 **완전 자기완결 single-file HTML**이다([#6](https://github.com/picpal/dev-crew/issues/6)): CSS/JS 전부 인라인, 이미지는 data URI 또는 인라인 SVG, 외부 요청 0(CDN·외부 폰트·외부 스크립트 금지).
- 사용자 입력과 Agent 출력은 전부 HTML escape 후 삽입하고, raw HTML 삽입 경로를 템플릿에서 구조적으로 제거한다(텍스트 슬롯만 제공).
- Worker가 응답에 CSP 헤더를 부착한다: `default-src 'none'; style-src 'unsafe-inline'; img-src data:;` 기본, JS가 필요한 템플릿 버전만 `script-src 'unsafe-inline'` 추가. 외부 fetch/XHR은 어떤 버전에서도 불허. CSP 변경은 templateVersion bump로 추적한다.

#### Cloudflare R2 Publisher / Hosting Adapter

- object key와 content type을 정규화한다.
- 업로드 후 content hash와 object metadata를 검증한다.
- transient failure를 bounded retry하고 permanent failure는 Report Service에 반환한다.
- Cloudflare 구현을 Adapter 뒤에 두어 향후 다른 object storage로 교체할 수 있게 한다.

#### Cloudflare Worker

- `GET /tasks/{taskId}`를 R2 object에 매핑한다.
- 존재하지 않는 Task는 명확한 404를 반환한다.
- HTML에 적절한 content type과 cache policy를 적용한다.
- 접근 정책은 **identity-aware**다([#5](https://github.com/picpal/dev-crew/issues/5)): Cloudflare Access(Zero Trust)를 Worker 앞단에 두고 IdP는 GitHub SSO 또는 email OTP, 정책은 운영자 계정 화이트리스트. Report에 security finding 등 repo 공개 범위를 넘는 내용이 담기므로 public은 성립하지 않고, Slack 링크를 임의 네트워크에서 여는 패턴 때문에 network-restricted도 배제한다. Access 인증과 무관하게 redaction 규칙(16.4절)은 유지한다.

### 15.5 Object와 URL 규칙

MVP 권장 규칙은 다음과 같다.

```text
R2 object key:
reports/{taskId}/index.html

External URL:
https://report.example.com/tasks/{taskId}
```

동일 Task의 재실행 이력을 보존해야 하는 단계에서는 다음으로 확장한다.

```text
reports/{taskId}/{executionId}/index.html
reports/{taskId}/latest.json

/tasks/{taskId}
/tasks/{taskId}/executions/{executionId}
```

MVP는 latest report overwrite를 허용하되 Publication Event와 GitHub Issue에 execution ID와 content hash를 남긴다. Audit 요구가 높아지면 versioned object를 기본으로 전환한다.

### 15.6 Report 화면 내용

- Task status, complexity, risk, lead time
- Workflow graph와 timeline
- Agent Instance별 role/provider/model/effort
- Developer 병렬 작업과 worktree scope
- Local Review 및 Integration Review 결과
- Stateful loop와 invalidation 이력
- MODEL_ESCALATION chain과 전후 token/latency/result
- 주요 의사결정과 rejected alternatives
- changed files와 build/test 결과
- Security 및 QA 결과
- GitHub Issue와 PR 링크
- aggregate tokens, human touch, handoff, queue wait

Raw prompt, secret, credential, 민감한 source excerpt는 Report에 포함하지 않는다.

## 16. 오류 처리와 운영 안전장치

### 16.1 Task와 Report 결과 분리

- 코드 작업이 성공하고 Report publish만 실패하면 Task 결과는 `DONE_REPORT_PENDING`으로 표시한다.
- Report Service는 재시도 queue에 넣고 Slack에는 GitHub Issue/PR 링크와 report 지연 상태를 알린다.
- publish 성공 후 Slack의 최종 링크를 갱신하거나 follow-up 메시지를 보낸다.

### 16.2 Idempotency

- Slack event 중복 수신 방지
- Task creation 중복 방지
- GitHub Issue 생성 중복 방지
- 동일 execution의 report publication 중복 방지
- R2 upload는 content hash로 동일 artifact를 검증

### 16.3 취소와 중단

- 사용자 취소는 Orchestrator가 downstream node와 active session에 전달한다.
- 이미 생성된 worktree와 artifact는 즉시 삭제하지 않고 recovery policy에 따라 보존한다.
- 취소된 Task는 GitHub Issue와 Trace에 종료 이유를 남긴다.

### 16.4 보안과 비밀정보

- provider credential, Slack token, GitHub token, Cloudflare credential은 secret store 또는 환경 주입을 사용한다.
- Agent output과 test log에서 secret을 redact한 뒤 Report Generator에 전달한다.
- R2 bucket의 direct public access는 사용하지 않고 Worker를 단일 serving layer로 둔다.
- 외부 공유 범위가 팀 내부라면 Worker auth를 MVP POC의 필수 결정 항목으로 둔다.

## 17. 설정 예시

```yaml
providers:
  claudeCode:
    adapter: claude-code-adapter
    modelTiers:            # tier = (full model ID, 기본 effort) 쌍. alias 금지 (#12)
      CHEAP:            { model: claude-sonnet-5, effort: LOW }
      DEFAULT:          { model: claude-sonnet-5, effort: HIGH }
      HIGH_CAPABILITY:  { model: claude-opus-5,   effort: HIGH }   # routing이 XHIGH 승격 가능

  codex:
    adapter: codex-adapter
    modelTiers:
      CODEX_DEFAULT:        { model: gpt-5.6-terra, effort: MEDIUM }
      CODEX_HIGH_REASONING: { model: gpt-5.6-sol,   effort: HIGH } # routing이 XHIGH 승격 가능

roleDefaults:
  orchestrator: { provider: CLAUDE_CODE, tier: DEFAULT, effort: MEDIUM }
  explorer:     { provider: CLAUDE_CODE, tier: CHEAP, effort: MEDIUM }
  architect:    { provider: CLAUDE_CODE, tier: DEFAULT, effort: MEDIUM }
  developer:    { provider: CLAUDE_CODE, tier: DEFAULT, effort: MEDIUM }
  security:     { provider: CLAUDE_CODE, tier: DEFAULT, effort: HIGH }
  qa:           { provider: CLAUDE_CODE, tier: CHEAP, effort: MEDIUM }
  reviewer:     { provider: CODEX, tier: CODEX_DEFAULT, effort: MEDIUM }

modelRouting:
  policyVersion: v1
  strategy: CHEAP_DEFAULT_FIRST
  escalationCreatesNewInstance: true

hosting:
  adapter: cloudflare-r2
  objectKeyPattern: reports/{taskId}/index.html
  publicUrlPattern: https://report.example.com/tasks/{taskId}
  access: cloudflare-access        # identity-aware (#5)

slack:
  mode: socket                     # Socket Mode (#8)
  tokens: [SLACK_BOT_TOKEN, SLACK_APP_TOKEN]   # 환경 주입

store:
  trace: trace.db                  # append-only events + projections (#7)
  registry: harness.db             # 활성-only Session Registry (#15)
```

model tier 매핑은 설정이 소유하되 full model ID를 pin한다 — alias는 provider·시점에 따라 해석이 달라져 ModelRoutingEvent 재현성을 훼손한다. Role prompt에는 model 이름을 하드코딩하지 않는다. 미채택 기록: Haiku 4.5(effort 미지원), Fable 5(2배 비용·ZDR 불가·비대화형 무동의 과금 — Phase 2 escalation 최상단 후보), gpt-5.6-luna(미할당), gpt-5.4 계열(2026-08-31 은퇴).

## 18. MVP와 향후 단계

### Phase 0 — 기술 POC

목표는 설계 가정의 실행 가능성을 빠르게 검증하는 것이다.

- Claude Code Adapter에서 start/send/resume/cancel/usage 수집 검증
- Codex Adapter에서 Reviewer thread/session lifecycle과 model/effort 지정 검증
- Role별 tool/permission enforcement 검증
- 동일 repository에서 독립 Developer session + worktree 검증
- Model Routing 결정과 MODEL_ESCALATION 새 instance spawn 검증
- 정적 HTML 1개 생성 → R2 upload → Worker `/tasks/{taskId}` 조회 시간 검증
- Slack에 외부 Report URL과 GitHub link를 보내는 end-to-end 검증
- Harness 재시작 후 session recovery 검증

### Phase 1 — MVP

```text
Slack
  ↓
Orchestrator
  ↓
Explorer → Developer(s) → Reviewer Queue → QA
  ↓
GitHub Issue/PR
  ↓
Static HTML → R2 → Worker URL → Slack
```

MVP 범위:

- 승인된 SMALL/MEDIUM Workflow Template
- Orchestrator, Explorer, Developer, Reviewer, QA
- 고위험 Task에서 Architect와 Security 조건부 투입
- Developer workload-driven scaling, 최대 3
- Reviewer queue-driven scaling, 기본 1
- Local Review 필수, dependency가 있으면 Integration Review
- Stateful loop 최대 5회와 no-progress detection
- Role별 provider 기본값과 complexity/risk/role model routing
- MODEL_ESCALATION 1단계 이상
- append-only trace와 기본 aggregate
- GitHub Task Report
- Cloudflare R2 + Worker 기반 외부 HTML Report URL

MVP에서 GitHub Pages, Cloudflare Pages build, localhost-only Report Server는 사용하지 않는다.

### Phase 2 — 안정화와 평가

- benchmark task 20~30개 AS-IS/TO-BE pair 실행
- cohort별 model routing 효과 분석
- Reviewer queue autoscaling threshold 조정
- context CONTINUE/COMPACT/ROTATE 효과 측정
- Report versioned objects와 execution별 URL
- Worker access control, retention, cache 정책 확정
- Slack link unfurl 또는 richer preview 검토
- Architecture/Security/QA workflow template 확장

### Phase 3 — 확장

- 데이터 기반 routing rubric 자동 보정
- provider outage fallback policy
- 비교 Report와 dashboard
- `/tasks/{taskId}/trace`, `/compare/{benchmarkId}`, `/dashboard` 확장
- In-Agent Subagent parallelism POC
- 다중 repository와 조직별 policy

## 19. Architecture Review

### 19.1 확정된 결정

| 항목 | 결정 | 근거 |
|---|---|---|
| 사용자 창구 | Orchestrator 단일 창구 | 상태와 책임 일관성 |
| 통신 | Hub-and-Spoke | Worker mesh와 context 혼란 방지 |
| 역할 실행 | Role과 Instance 분리 | 독립 session, retry, model assignment, trace |
| 병렬 개발 | Developer Instance + Worktree | 변경과 review 격리 |
| Reviewer | Codex provider + queue | 독립 review와 비용/병목 균형 |
| 나머지 역할 | Claude Code provider | 기존 역할 실행 기반 유지 |
| Review | Local + 조건부 Integration | 빠른 피드백과 통합 결함 탐지 |
| Workflow | Stateful loop | 실제 재작업과 invalidation 표현 |
| Model | complexity + risk + role routing | 작업 크기만으로 선택하는 오류 방지 |
| Escalation | 새 Agent Instance spawn | model attribution과 observability 보존 |
| Audit | GitHub Issue/PR 유지 | 영구 기록과 코드 diff 연결 |
| 상세 Report | 외부 HTML URL | Slack 가독성과 drill-down |
| Hosting | Cloudflare R2 + Worker | full-site rebuild 없이 즉시 publish |
| GitHub Pages | 사용하지 않음 | 배포 지연이 요구와 불일치 |
| Adapter 표면 | 양 provider 공식 Python SDK | 6.2절 연산의 네이티브 커버리지, CLI는 폴백 (#11) |
| effortLevel | 5단계 (XHIGH 추가) | 양 provider 실제 옵션과 1:1, 4단계는 정보 손실 (#12) |
| model tier | full model ID pin | alias는 재현성 훼손; 매핑은 17절 (#12) |
| Integration Review | 항상 새 instance | anchoring 제거, tier는 risk가 결정 (#4) |
| capability 강제 | 선언적 policy → provider 네이티브 변환 | 콜백 거부 + read_only sandbox + MCP 한정 (#13) |
| Trace Store | SQLite(WAL), events+projection | trace.db/harness.db 수명 주기 분리 (#7) |
| Session Registry | 활성-only + lazy verify | 재시작 시 LLM 호출 0회, 인계는 7.6절 스키마 재사용 (#15) |
| Slack 연결 | Socket Mode | 로컬 우선, 공개 endpoint 불요 (#8) |
| Report 접근 | Cloudflare Access (identity-aware) | security finding 포함으로 public 불성립 (#5) |
| Report HTML | single-file + 엄격 CSP | 외부 요청 0, R2 단일 object와 정합 (#6) |

### 19.2 주요 위험과 완화

| 위험 | 영향 | 완화 |
|---|---|---|
| Orchestrator가 직접 구현까지 수행 | context 오염, 역할 붕괴 | tool allowlist와 hook enforcement |
| 자유형 graph 과설계 | 구현 복잡도와 디버깅 비용 증가 | 승인된 template로 MVP 시작 |
| Developer 과도한 병렬화 | merge 충돌, 토큰 증가 | independence/value rubric와 max concurrency |
| Reviewer queue 병목 | lead time 증가 | queue wait 기반 scale-out |
| 최고 모델 남용 | 비용 증가 | cheap/default-first와 routing event |
| 부적절한 낮은 모델 | 반복 실패 | no-progress + MODEL_ESCALATION |
| session 중 model 변경 | 품질 attribution 상실 | 새 Agent Instance 생성 |
| Report에 민감 정보 노출 | 보안 사고 | redact, sanitize, Worker access policy |
| Report publish 실패 | Slack 진입점 부재 | DONE_REPORT_PENDING과 독립 retry |
| R2 overwrite로 이력 손실 | Audit 약화 | Publication Event, content hash, 향후 versioned object |
| AS-IS/TO-BE 관측 수준 차이 | 평가 왜곡 | 공통 event schema와 동일 acceptance criteria |

### 19.3 남은 Architecture 결정

Phase 0 착수를 막던 결정은 [Wayfinder Map #1](https://github.com/picpal/dev-crew/issues/1)에서 전부 닫혔다(19.1절에 반영). 남은 항목은 Phase 2 운영 정책이다.

- R2 report retention과 삭제 정책
- MVP에서 Task latest overwrite를 허용하는 기간과 versioned object 전환 시점

POC 실측으로 확인할 항목: 실제 적용 effort의 관측 경로(org effort limit clamp 무보고 문제), Reviewer Queue scale signal 임계값.

loopPolicy 토큰 budget의 타당성은 실측으로 답이 나왔다(2026-08-20): opus HIGH leader +
Codex 리뷰어 구성에서 1회 실행이 587k를 썼다 — 기존 300k는 정상 완료를 강제 종료시키는
값이었다. hard cap을 1.5M으로 올리고, role별 예산은 종료 트리거가 아닌 경보로 강등했다
(§10.3).

## 20. POC 검증 항목과 성공 기준

| POC | 성공 기준 |
|---|---|
| Claude Code session lifecycle | 새 session, follow-up, resume, cancel, usage 수집이 안정적으로 동작 |
| Codex Reviewer lifecycle | 지정 model/effort로 review instance 생성 및 resume 가능 |
| Provider role enforcement | Reviewer가 Codex, 다른 기본 역할이 Claude Code로 생성됨 |
| AgentInstance schema | provider/model/effort/routing reason이 모든 spawn에 저장됨 |
| Model Routing | LOW/MEDIUM/HIGH 사례에서 rubric대로 tier 선택 |
| MODEL_ESCALATION | 기존 instance 종료 상태 보존 후 새 instance가 artifact를 인계받음 |
| Dynamic Developer | 2~3개 독립 worktree가 충돌 없이 병렬 실행 |
| Reviewer Queue | queue wait 측정과 threshold 기반 추가 Reviewer 생성 |
| Stateful Loop | NOT_PASS → 수정 → 재검증, 최대 5회와 invalidation 동작 |
| Recovery | Harness 재시작 후 active workflow와 session 상태 복구 |
| Observability | task→workflow→instance→turn→tool/model event 추적 가능 |
| GitHub Report | 시작 Issue와 완료 summary/PR/report link 갱신 |
| HTML Report | workflow, model, loops, metrics, decisions가 정적 HTML로 표현 |
| R2 Publisher | Task report object 하나를 전체 rebuild 없이 업로드 |
| Worker Serving | `/tasks/{taskId}`가 업로드 직후 외부에서 정상 조회 |
| Slack Delivery | 요약 + report link + Issue/PR link가 하나의 완료 메시지로 전달 |
| Security | secret redaction, HTML sanitization, 접근 정책이 검증됨 |

MVP end-to-end 성공 기준은 다음과 같다.

```text
Slack 요청
  → Task/Issue 생성
  → 올바른 provider/model로 Agent spawn
  → 필요 시 병렬 Developer와 Reviewer queue 실행
  → loop/escalation 관측
  → QA 완료
  → GitHub Report 갱신
  → Static HTML 생성 및 R2 upload
  → Worker URL 외부 조회
  → Slack에 Summary + Report + Issue/PR links 전달
```

## 21. 구현 우선순위

1. TaskExecution, WorkflowExecution, AgentInstance, Event schema
2. Claude Code Adapter와 Codex Adapter 공통 계약
3. Orchestrator와 Role capability enforcement
4. Dispatcher, Session Registry, Worktree Manager
5. Model Routing Policy와 routing event
6. Developer scaling과 Reviewer Queue
7. Local/Integration Review와 Stateful Loop
8. Trace/Metrics Store와 GitHub Task Report
9. Report Generator와 HTML Renderer
10. Cloudflare R2 Publisher와 Worker route
11. Slack 완료 메시지와 end-to-end recovery
12. Benchmark와 routing/queue tuning

## 22. 최종 불변 조건

다음 조건은 구현 과정에서 변경하려면 별도 Architecture Review가 필요하다.

1. User-facing communication은 Orchestrator만 담당한다.
2. Orchestrator는 Control Plane이며 repository 직접 실행 capability를 갖지 않는다.
3. Reviewer의 기본 adapter/provider는 Codex다.
4. Orchestrator, Explorer, Architect, Developer, Security, QA의 기본 adapter/provider는 Claude Code다.
5. Agent spawn은 role, provider, model, effort/reasoning level을 명시한다.
6. Model 선택은 complexity, risk, role을 함께 평가한다.
7. MODEL_ESCALATION은 같은 session의 model 변경이 아니라 새 Agent Instance 생성으로 수행한다.
8. Developer는 workload-driven scaling, Reviewer는 queue-driven scaling을 사용한다.
9. 병렬 개발 결과는 Local Review를 거치며 필요한 경우 Integration Review를 추가한다.
10. Workflow는 bounded loop, invalidation, escalation을 지원한다.
11. Raw trace, GitHub Audit Trail, HTML Report의 책임을 분리한다.
12. Slack에는 HTML 자체가 아니라 외부 Report URL을 전달한다.
13. Report Hosting은 Cloudflare R2 + Worker를 사용하며 GitHub Pages를 사용하지 않는다.
14. Task별 Report publish는 전체 사이트 rebuild 없이 수행한다.
15. Token, latency, quality는 provider/model/effort/role/complexity/risk 차원으로 관측한다.
