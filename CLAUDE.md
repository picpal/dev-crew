# dev-crew

Slack 요청을 Claude Code / Codex Agent로 분해·실행·검증하고, 결과를 GitHub와 외부 HTML Report로 제공하는 로컬 우선 Multi-Agent Engineering Harness.

설계의 기준 문서는 [DESIGN.md](./DESIGN.md)다. 구현, POC, 운영 정책, 평가 기준은 이 문서를 우선한다.

작업을 시작하기 전에 [lessons.md](./lessons.md)를 읽는다 — 이 저장소에서 LLM이 실제로
반복한 오판을 유형으로 모아둔 문서다. 새로 발견한 오판은 그 카테고리에 추가한다.

## Agent skills

### Issue tracker

GitHub Issues (`picpal/dev-crew`), `gh` CLI로 조작한다. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context — 루트 `CONTEXT.md`와 `docs/adr/`. See `docs/agents/domain.md`.
