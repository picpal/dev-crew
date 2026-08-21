# TUTOR — 학습 Agent 설계

이슈: [#19](https://github.com/picpal/dev-crew/issues/19) · 결정 근거는 그 이슈의 결정 기록과 ruling 코멘트.

## 목적

지정한 repo의 **프로세스와 핵심 내용에 대한 이해도를 높인다.** 10문항 4지선다를 풀고,
채점 결과를 영역별 카드 리포트로 받는다. 오답은 누적되고 맞히면 지워진다.

이 Agent의 성패는 문항의 정확성 하나에 달려 있다. **거짓을 가르치는 학습 도구는 없느니 못하다** —
그래서 근거 강제와 교차 검증이 설계의 중심이고, 나머지는 그 위에 얹힌다.

## 역할과 provider

| Role | provider/tier | 책임 |
|---|---|---|
| `TUTOR` | `HIGH_CAPABILITY` (Claude) | 출제·해설. worktree에서 읽기 도구로 근거를 찾는다 |
| `TUTOR_VERIFIER` | `CODEX_DEFAULT` (Codex) | 문항 판정. **출제와 provider를 가른다** |

교차 검증의 전제는 provider 분리다. 같은 모델 계열은 같은 방식으로 틀리므로, 출제자가 놓친
확장·비약을 같은 계열 검증자도 놓친다. 이 repo가 이미 쓰는 방식이다 (`REVIEWER: CODEX_DEFAULT`).

`TUTOR`는 워크플로 노드가 아니라 `BRAIN`과 같은 **대화형 세션 역할**이다.

## 출제 파이프라인

```
                 ┌──────────────── 보충 1회 ────────────────┐
                 ▼                                          │
  spawn TUTOR ──▶ 12문항 + evidence ──▶ ① 인용 대조 ──▶ ② spawn TUTOR_VERIFIER
   (worktree)        구조화 출력          하네스/결정적        Codex/문항별 판정
                                              │                     │
                                          폐기 ◀── 없는 인용    REJECT ─┘
                                                                 │
                                                          PASS 상위 10 ──▶ QuizIssuedEvent
```

### ① 인용 대조 — 결정적, LLM 없음

`evidence[i]`의 `path`를 worktree 기준으로 읽어 `start_line..end_line` 범위에 `quote`가
실제로 있는지 대조한다. 공백 정규화 후 부분 문자열 비교.

폐기 사유: 경로 없음 / 범위 밖 / quote 불일치 / evidence 배열이 비었음 /
worktree 밖을 가리키는 경로(`..`, 절대경로).

evidence의 `minItems`는 스키마에 두지 않는다 — provider strict 모드가 지원하지 않는 키워드가
있고, 어차피 **하네스가 버리는 것**이 D2의 요지다. 스키마는 형태만, 판정은 하네스가 한다.

### ② 검증자 — 별도 인스턴스, 다른 provider

투입: 대상 repo(worktree, 읽기 도구) + 문항·보기·정답·evidence·해설.
**투입하지 않는 것**: 출제 과정의 논증과 대화 이력. 출제자의 근거를 보면 그대로 수긍한다.

문항별 `PASS`/`REJECT` + 사유. 판정 기준 4개:

1. evidence가 정답을 실제로 뒷받침하는가
2. 오답 보기 3개가 근거에 비추어 **명백히** 틀린가 (애매하면 반려)
3. repo에 없는 일반 지식으로 확장하지 않았는가
4. 해설의 주장·수치가 전부 근거에서 나오는가

### ③ 통과분에서 10문항 고르기

우선순위대로 자른다.

1. **오답 노트에서 유래한 문항** (최대 3)
2. `area`가 아직 안 뽑힌 문항 — 한 영역에 몰리지 않게
3. 출제 순서

### ④ 부족분 처리

12문항 중 통과가 10 미만이면 **보충 출제 1회**(반려 사유를 함께 투입). 그래도 미달이면
**채운 만큼만 출제하고 사실대로 알린다.** 숫자를 맞추려다 지어내는 것이 이 Agent의 최악 실패다.

## 데이터

### 문항 (TUTOR 출력)

```
questions[]:
  area              문항이 다루는 영역 (모델이 태깅, 회차당 5개 이하)
  type              CORRECT | INCORRECT   — 옳은 것 고르기 / 틀린 것 고르기
  stem              지문
  options[4]        보기
  answer_index      0..3
  evidence[]        {path, start_line, end_line, quote}   — 정답의 근거
  explanation       해설
  diagram           도식 스펙 | null                       — 아래
```

### 도식 스펙 — 모델은 데이터만, 그림은 하네스가

모델이 인라인 SVG를 직접 쓰지 않는다. 이유는 셋이다.

| | 모델이 SVG 작성 | 하네스가 렌더 (채택) |
|---|---|---|
| escape 원칙 (#6) | raw HTML 삽입 경로가 생겨 깨진다 | 텍스트 슬롯만 유지 |
| 근거 검증 | SVG 안 수치를 evidence와 대조할 수 없다 | JSON 값이라 대조된다 |
| 실패 모드 | 깨진 SVG가 그대로 게시 | 렌더 실패 시 표로 폴백 |

지원 타입 2종으로 시작한다.

```
diagram = {"type": "bar",  "items": [{"label", "value"}], "unit"}
        | {"type": "flow", "nodes": [{"id", "label"}], "edges": [{"from", "to", "label"}]}
```

알 수 없는 타입이나 렌더 실패는 **표로 폴백**한다 — 그림을 못 그렸다고 해설을 잃지 않는다.

## 오답 노트

새 스토어를 만들지 않는다. trace store가 append-only 이벤트 로그이고 `execution_id`로
조회되므로 그대로 쓴다.

```
execution_id = TUTOR-{slack_user}-{repo_name}
```

| 이벤트 | 시점 | payload |
|---|---|---|
| `QuizIssuedEvent` | 출제 확정 | 문항 세트 전체 (세션 유실 후 재개의 근거) |
| `QuizAnswerEvent` | 답변마다 | `{q_key, choice, correct}` |
| `QuizMissEvent` | 채점 시 오답 | `{q_key, area, evidence}` |
| `QuizClearedEvent` | 채점 시 오답 문항 정답 | `{q_key}` |

**현재 오답 = `QuizMissEvent` 중 그 뒤에 같은 `q_key`의 `QuizClearedEvent`가 없는 것.**
id 순서로 판정한다 (벽시계가 아니라 rowid — brain의 `_prior_handoff`가 같은 이유로 그렇게 한다).

### 문항 동일성 키

"같은 문항을 맞히면 오답에서 제거"의 *같은 문항*은 문자열 일치가 아니다. 지문을 바꿔 다시
물어도 같은 지점을 묻는 것이면 같다.

```
q_key = sha256(area + "|" + 정렬된 evidence의 (path, start_line))[:12]
```

**재출제 문항은 원래 키를 들고 다닌다.** 키를 매번 재계산하면 모델이 같은 지점을 40–52로
인용했다가 다음에 38–55로 인용하는 순간 키가 갈라져 오답이 영원히 안 지워진다. 오답 노트에서
뽑아 낸 문항에는 하네스가 원본 `q_key`를 붙여 두고, 해소 판정은 **붙여 둔 키로** 한다.
새로 만든 문항만 키를 계산한다.

### 재출제

한 회차 10문항 중 **최대 3문항**을 오답 노트에서 뽑는다. 별도 라운드가 아니라 12문항 출제
지시에 포함된다 — 오답 evidence를 함께 투입하고 "이 근거로 다시 출제하라"고 지시한다.
같은 문항을 그대로 내지 않고 **같은 evidence로 새로 출제**해 답 외우기를 막는다.
오답이 3개 미만이면 있는 만큼만.

## Slack 흐름

`@tutor`는 **별도 Slack 앱**이다 (`TUTOR_BOT_TOKEN`/`TUTOR_APP_TOKEN`). brain과 같은 패턴 —
인터뷰 세션과 퀴즈 세션이 한 스레드 공간을 쓰면 명령이 충돌한다.

```
@tutor message-gate:            ← repo registry(#16) 접두
  ↓  출제 파이프라인 (수십 초)
스레드에 1문항씩 — 보기 4개 버튼 (brain question_blocks 패턴)
  ↓  진행 중 정답 비공개
10문항 완료 → 채점 → 리포트 URL
```

brain에서 그대로 가져오는 것: 세션 수명·소유자 권한(`_may_command`)·Block Kit 버튼·
`_dedupe`·리포트 발행. 재구현하지 않고 공용화하거나 같은 형태로 복제한다.

### 중단·재개

문항 세트가 `QuizIssuedEvent`에 있고 답변이 `QuizAnswerEvent`에 있으므로, 세션이 죽어도
스레드 답글/버튼으로 이어 풀 수 있다. 미완 회차는 **24시간**이 지나면 무효로 본다.

## 리포트

`report/quiz_report.py` — 기존 `report/uploader.py` 발행 경로 재사용.

- **JS 0.** 클릭 펼침은 `<details>/<summary>`, 차트는 하네스가 만든 인라인 SVG.
  Worker의 CSP 분기(`script-src`)는 이 작업 범위 밖이다.
- 문항 하나 = 카드 1개. **영역(`area`)별로 그룹**해서 배치.
- 카드 앞면: 지문 + 내가 고른 보기 + 정오. 펼치면: 정답·해설·도식·근거(`path:line`).
- 상단 요약: 총점, 영역별 정답률(막대 SVG), 이번 회차 오답 노트 변화(추가/해소).
- 모든 삽입은 escape. raw HTML 슬롯을 만들지 않는다.

## 파일 배치

```
roles/tutor/{prompt.md,output.schema.json}
roles/tutor_verifier/{prompt.md,output.schema.json}
src/devcrew/quiz.py            문항 모델·q_key·인용 대조·채점·오답 노트 질의
src/devcrew/slack_tutor.py     세션·Slack 흐름 (slack_brain 대응)
src/devcrew/report/charts.py   도식 스펙 → 인라인 SVG (+ 표 폴백)
src/devcrew/report/quiz_report.py  카드 리포트 렌더
config/harness.yaml            roleDefaults에 TUTOR / TUTOR_VERIFIER
src/devcrew/schema.py          Role enum 확장
src/devcrew/slack_engine.py    @tutor 앱 배선
```

## 테스트 전략

LLM 없이 검증 가능한 부분이 대부분이다 — 거기에 테스트를 집중한다.

| 대상 | 방식 |
|---|---|
| 인용 대조 | 실제 임시 파일에 대고 순수 함수 테스트. 없는 경로·범위 밖·quote 불일치·`..` 탈출 |
| `q_key` | 지문이 달라도 같은 evidence면 같은 키, 다른 evidence면 다른 키 |
| 오답 노트 질의 | trace에 이벤트를 직접 넣고 miss/cleared 상호작용 (특히 miss→clear→miss 순서) |
| 채점 | 답변 배열 → 점수·영역별 정답률 |
| 파이프라인 | `FakeAdapter`로 출제·검증 응답을 스크립트. 반려→보충→미달 경로 포함 |
| SVG 렌더 | 스펙 → 문자열. 알 수 없는 타입이 표로 폴백하는지 |
| Slack 흐름 | `slack_brain` 테스트 하네스 재사용 (SaySpy/BlockSaySpy) |

## 범위 밖

- Worker CSP의 템플릿별 분기 (#6의 미구현 분기) — 필요해지면 별도 이슈
- 학습 진도·통계 대시보드 — 회차 리포트가 먼저다
- 여러 repo 교차 출제 — 한 회차는 한 repo
