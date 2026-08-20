# Orchestrator Workflow Engine — Design Spec

> Date: 2026-08-18 · Branch: feat/orchestrator-loop (base ef1779f)
> 상위 문서: DESIGN.md v0.5.0 §3.4, §3.5, §5.2, §10, §12.1
> 승인: 사용자 (grilling 결정 일괄, 2026-08-18)

## 목적

§10 Stateful Workflow의 상태 머신을 **결정적 엔진**으로 구현하고, 판단이 필요한
지점에서만 Orchestrator LLM을 호출한다. §3.4의 Orchestrator 전용 MCP tool
(읽기 전용 조회)을 노출한다. agent-roles effort에서 park한 event attribution
결함(#18)을 이 재설계에서 해소한다.

## 범위

- 포함: 워크플로 템플릿 + 전이 엔진 + loop guard, LLM 결정 지점, Orchestrator
  role bundle, MCP 읽기 전용 조회 tool 3종, p09 live 스모크
- 제외: Dispatcher 동시성 관리(§12.2), Task Knowledge(§11.2), Slack 연계 — 후속 effort

## 결정 사항

1. **주도권은 엔진.** §10.1/§10.2가 답을 정해둔 전이(PASS→NEXT, Reviewer
   NOT_PASS→해당 Developer 재투입, QA FAIL→Developer 복귀 등)는 엔진이 0-토큰
   즉시 처리. LLM 결정 지점은 열거형으로 고정: `CLASSIFY`(실행 시작 시 조건부
   노드 생략 결정), `NEED_REPLAN`, `BLOCKED`, `INSUFFICIENT_CAPABILITY`,
   `LOOP_GUARD_EXCEEDED`. 구분 기준은 "정책이 이미 답을 정해뒀는가"다.
2. **템플릿은 Python 선언** (`src/devcrew/workflow.py`, dataclass). 기본 템플릿
   1개: `EXPLORE(조건부) → DEVELOP → REVIEW(Dev↔Review 루프) → QA(FAIL 시 DEVELOP
   복귀)`. loopPolicy 수치만 `config/harness.yaml`로: maxIterations 5,
   maxDurationMinutes 60, maxTokenBudget 300000(→ 2026-08-20 실측 후 1500000으로
   개정, DESIGN.md §10.3), sameFindingEscalationThreshold 3
   (§10.3). guard 초과는 Task 실패가 아니라 `LOOP_GUARD_EXCEEDED` 결정 지점 진입.
3. **LLM 결정은 fresh 세션 + 구조화 출력.**

   > **개정 2026-08-20.** 이 결정은 `leaderContext.persistent`로 선택 가능해졌다.
   > 기본값은 `true`(스레드 단위 지속 세션 + 창 50%에서 자체 요약 compaction) —
   > "이어서 고쳐줘" 류 후속 요청마다 leader가 맥락을 다시 쌓는 비용을 없애기
   > 위해서다. `false`면 아래 원문대로 결정마다 fresh 세션이다. 스냅샷 주입·구조화
   > 출력·허용 목록 대조 검증은 두 모드에서 동일하다. 결정 스키마에는
   > `report`(nullable) 필드가 추가됐다 — 실행 종료 시 사용자용 보고문 전용이며
   > 결정 turn에서는 항상 null이다. 상세는 DESIGN.md §10.4.

   원문: 엔진이 상태 스냅샷(실행
   이력 요약, 트리거 Worker 결과 전문, 허용 action 목록)을 initial message로
   패키징해 새 ORCHESTRATOR instance를 spawn (기존 spawn/start_worker 배관 재사용).
   구조화 출력이 곧 결정 — 별도 제출 tool 없음. 결정 스키마:
   `{status, summary, decision: {action, target_node, rationale}}`,
   action enum: `PROCEED | RETRY_NODE | ESCALATE_MODEL | SKIP_NODE | REPLAN | ASK_USER | ABORT`
   (`PROCEED`는 CLASSIFY 등에서 "템플릿대로 진행" 결정, `target_node`는 nullable).
   엔진은 결정을 허용 목록과 대조 검증 후 적용한다. 허용 밖 action·malformed는
   1회 재시도 후 `ASK_USER`(HUMAN)로 강등 — fail-closed.
4. **Orchestrator role bundle 신설**: `roles/orchestrator/{prompt.md,
   output.schema.json}` (한국어 4절, OpenAI-strict 규칙과 동일 강도의 스키마).
   Orchestrator는 Claude provider 고정(§5.1), tier는 HIGH_CAPABILITY 기본.
5. **MCP는 읽기 전용 조회 3종**, `create_sdk_mcp_server` 인프로세스 서버:
   `get_execution_state(execution_id)`, `get_worker_result(instance_id)`,
   `get_trace_events(execution_id, event_type?, limit)`. Orchestrator role
   policy는 이 3 tool만 allowlist에 둔다(§3.4 — repo tool 일절 없음). 스냅샷이
   부족할 때만 LLM이 끌어온다.
6. **Event attribution 해소**: 엔진은 각 turn의 (생산자 instance, TurnOutcome)
   쌍을 유지하고 `consume_result`를 항상 **결과를 생산한 instance**로 호출한다.
   `WorkerResultEvent`/`LoopEvent`가 생산자 instance_id로 기록된다 — 기존
   `run_review_loop`가 Reviewer 결과를 dev_inst로 기록하던 결함(#18 park) 해소.
   `run_review_loop`는 엔진 루프로 대체하고 제거한다.
7. **엔진 상태는 in-memory + trace append.** `NodeTransitionEvent`,
   `DecisionEvent`를 trace.db에 기록해 사후 재구성 가능하게 한다. 실행 중단
   복구(projection 재구성)는 후속 effort.
8. **오류는 기존 관례 유지**: 설정·번들 누락 fail-fast, 신뢰 불가 출력 fail-closed.

## 검증 기준

- unit: 전이 표 전수(role×status), loop guard 3종(iterations, same-finding,
  token budget), 결정 검증·거부·강등, 템플릿 무결성, MCP tool 핸들러,
  attribution(Reviewer 결과가 Reviewer instance_id로 기록됨을 회귀 고정)
- 시뮬: FakeAdapter로 기본 템플릿 3 시나리오 — 직행 PASS / NOT_PASS 2회 후
  PASS / guard 초과 → fake 결정 ESCALATE_MODEL
- live: p09 — 장난감 repo에서 Dev→Review(NOT_PASS)→재투입→PASS 미니 루프 +
  결정 지점 1회. CHEAP/CODEX_DEFAULT tier, 짧은 프롬프트

## 실행 조건 (사용자 지시, agent-roles와 동일)

- 사용자 테스트 가능 상태까지 자율 진행, 중간 확인 없음
- 최종 리뷰는 Codex(CODEX_HIGH_REASONING 상당), 수정 루프 최대 3회
- PASS 시 사용자에게 테스트 방법과 함께 통지
