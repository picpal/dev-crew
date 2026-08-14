# dev-crew — Multi-Agent Engineering Harness Design

> Status: Source of Truth  
> Project: dev-crew  
> Version: 0.4.1  
> Updated: 2026-08-14  
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
  effortLevel: LOW | MEDIUM | HIGH | MAX
  reasoningLevel: LOW | MEDIUM | HIGH | MAX
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

`effortLevel`은 provider 공통 정책 표현이다. Adapter는 이를 provider가 지원하는 실제 reasoning/effort 옵션으로 변환한다. `reasoningLevel`은 실제 요청에 적용된 정규화 값으로 기록해 provider 간 비교에 사용한다. 지원하지 않는 조합은 Adapter가 조용히 변경하지 않고 routing validation error로 반환해야 한다.

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

### 6.3 Session 복구

- Harness 재시작 후 Session Registry에서 active instance를 복구한다.
- provider session을 resume할 수 있으면 기존 instance를 재개한다.
- resume이 불가능하면 기존 instance를 `FAILED_RECOVERY`로 종료하고, 요약 artifact를 전달한 새 instance를 생성한다.
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
| Reviewer, Integration/high-risk Review | CODEX_HIGH_REASONING | HIGH | 독립 새 review instance 권장 |

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

Dependency와 shared contract가 없는 완전 독립 변경은 정책에 따라 Integration Review를 생략할 수 있다. 고위험 변경은 별도의 `REV-INTEGRATION-*` instance와 높은 reasoning 설정을 사용한다.

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
  maxTokenBudget: 300000
  sameFindingEscalationThreshold: 3
```

한도 초과는 Task의 즉시 실패가 아니라 자동 루프 종료와 Orchestrator escalation을 의미한다.

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

## 13. Observability와 Evaluation Architecture

### 13.1 원칙

- 모든 기록은 최종적으로 `taskId`와 `executionId`로 연결한다.
- Raw event는 append-only로 보존하고 aggregate는 재계산 가능하게 한다.
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
- 외부 asset 의존성을 최소화하거나 version-pinned asset을 사용한다.
- 사용자 입력과 Agent 출력은 escape/sanitize하여 script injection을 방지한다.

#### Cloudflare R2 Publisher / Hosting Adapter

- object key와 content type을 정규화한다.
- 업로드 후 content hash와 object metadata를 검증한다.
- transient failure를 bounded retry하고 permanent failure는 Report Service에 반환한다.
- Cloudflare 구현을 Adapter 뒤에 두어 향후 다른 object storage로 교체할 수 있게 한다.

#### Cloudflare Worker

- `GET /tasks/{taskId}`를 R2 object에 매핑한다.
- 존재하지 않는 Task는 명확한 404를 반환한다.
- HTML에 적절한 content type과 cache policy를 적용한다.
- 접근 정책이 private이면 identity/auth layer를 적용하고, public이면 민감 정보 비노출을 전제로 한다.

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
    modelTiers:
      CHEAP: ${CLAUDE_CHEAP_MODEL}
      DEFAULT: ${CLAUDE_DEFAULT_MODEL}
      HIGH_CAPABILITY: ${CLAUDE_HIGH_MODEL}

  codex:
    adapter: codex-adapter
    modelTiers:
      CODEX_DEFAULT: ${CODEX_DEFAULT_MODEL}
      CODEX_HIGH_REASONING: ${CODEX_HIGH_MODEL}

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
```

실제 model ID는 환경 설정에서 관리한다. 문서나 Role prompt에 특정 시점의 제품 model 이름을 하드코딩하지 않는다.

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

- Worker URL을 public, identity-aware, network-restricted 중 어떤 정책으로 제공할지
- R2 report retention과 삭제 정책
- MVP에서 Task latest overwrite를 허용하는 기간과 versioned object 전환 시점
- provider별 실제 model tier 매핑과 effort 호환성
- Reviewer Integration session을 항상 분리할지, high-risk에서만 분리할지
- HTML template의 asset bundling과 CSP 정책

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
