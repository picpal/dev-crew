# Agent Role 구성 — Design Spec

> Date: 2026-08-17 · Branch: feat/agent-roles (base d4f52ca)
> 상위 문서: DESIGN.md v0.5.0 §3.3, §3.4, §5.2, §10.1, §17
> 승인: 사용자 (권장안 일괄 채택, 2026-08-17)

## 목적

POC에서 raw 프롬프트로 구동하던 agent 세션에 **role 정의 번들**(지침 + 출력 계약)을 도입하고, §17 설정 파일 로딩을 구현한다. Orchestrator harness MCP tool은 별도 effort (범위 외).

## 범위

- Worker 4종: **Explorer, Developer, Reviewer, QA** (Architect·Security·Orchestrator 제외)
- role 지침(prompt.md, 한국어) + 출력 JSON Schema + config/harness.yaml 로딩 + spawn 주입 배관
- 검증: unit + role별 CHEAP live 스모크 1회 (`poc/p08_roles.py`)

## 구조

```text
roles/<role>/prompt.md           # §5.2 코드화: 책임 / 금지 / 작업 방식 / 보고 규칙
roles/<role>/output.schema.json  # 최종 보고 계약
config/harness.yaml              # §17: tiers, roleDefaults, concurrency — 설정이 정본
src/devcrew/roles.py             # RoleBundle(prompt, schema, version), load_bundle(role)
src/devcrew/config.py            # yaml 로드 → routing이 소비 (TIERS 하드코딩 제거)
poc/p08_roles.py                 # live 스모크
```

## 결정 사항

1. **출력 계약은 role별 JSON Schema, provider 네이티브 구조화 출력으로 강제.** 중간 turn은 자유, 최종 응답만 스키마 적용. 공통 골격: `status`(PASS|NOT_PASS|NEED_REPLAN|BLOCKED|INSUFFICIENT_CAPABILITY) + `summary`(string). role별 확장:
   - Explorer: `findings[{file, line, evidence, confidence}]`, `affected_files[]`
   - Developer: `changed_files[]`, `build{ok, detail}`, `tests{passed, failed, detail}`
   - Reviewer: `verdict`(PASS|NOT_PASS), `findings[{severity, file, line, description}]`
   - QA: `plan[]`, `results[{criterion, ok, repro}]`
2. **배관은 RoleBundle 로더 + spawn 파라미터 확장.** `Orchestrator.spawn`이 번들을 로드해 어댑터에 전달: `start_session(inst, msg, *, system_prompt=None, output_schema=None)` (하위호환). Adapter는 role을 모른다 — 전달만 한다.
   - Claude: `ClaudeAgentOptions(system_prompt=...)` + SDK 구조화 출력 (실제 옵션명은 구현 시 SDK 검증, T7 방식)
   - Codex: `thread_start(base_instructions=...)` + `run(output_schema=...)`
3. **trace 오염 방지**: AgentInstance에 `role_bundle_version: str | None` 1필드 추가 (기본값 None, 마이그레이션 불요). 프롬프트 원문은 instance/trace에 싣지 않는다.
4. **파싱**: `TurnOutcome.structured: dict | None` 추가. Orchestrator는 `structured["status"]`로 상태 전이. 스키마 검증은 provider 책임, harness는 방어적 처리만.
5. **오류는 fail-fast**: 번들 파일·yaml 누락 시 spawn 즉시 실패. 코드 폴백 없음 (§17 "설정이 정본", 조용한 기본값 금지 원칙과 일관).
6. **prompt.md 언어는 한국어**, 기술 용어는 영어 병기. 각 prompt는 4절 고정 구성: 책임 / 금지 / 작업 방식 / 보고 규칙 (보고 규칙은 스키마 필드 의미를 설명).

## 검증 기준

- unit: 번들 로딩(누락 시 에러 포함), 스키마 공통 골격 검증, yaml→TIERS 로딩(기존 routing 테스트 green 유지), FakeAdapter의 structured 전달, spawn이 bundle version 기록
- live: p08 — 장난감 repo에서 4 role 각 1회 (Explorer: 함수 위치 찾기 / Developer: 한 줄 수정 / Reviewer: diff 평가 / QA: 기준 확인). 각 출력이 스키마 적합 + status가 enum 값.

## 실행 조건 (사용자 지시)

- 사용자 테스트 가능 상태까지 자율 진행, 중간 확인 없음
- 최종 리뷰는 **Codex**(CODEX_HIGH_REASONING 상당)로 수행, 수정 루프 **최대 3회**
- PASS 시 사용자에게 테스트 방법과 함께 통지
