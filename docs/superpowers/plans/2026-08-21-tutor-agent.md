# TUTOR 학습 Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 지정 repo에 대해 근거가 검증된 10문항을 출제·채점하고, 영역별 카드 HTML 리포트와 오답 노트를 제공하는 Slack Agent를 만든다.

**Architecture:** 순수 로직(`quiz.py`) / LLM 파이프라인(`tutor.py`) / Slack 흐름(`slack_tutor.py`) 3층으로 가른다. 테스트 가능한 부분을 최대한 순수 함수로 밀어내고, LLM 왕복은 `FakeAdapter`로 스크립트한다. 출제는 Claude, 검증은 Codex로 provider를 가른다.

**Tech Stack:** Python 3.12, pytest/pytest-asyncio, slack_bolt AsyncApp, SQLite(TraceStore), Cloudflare Workers KV

**Spec:** `docs/superpowers/specs/2026-08-21-tutor-agent-design.md`

## Global Constraints

- 문항·해설은 대상 repo의 소스 코드·문서 근거에만 기반한다. 지어내기·확장 금지 (이슈 #19 D1)
- role output schema는 **provider strict 호환**: `additionalProperties: false`, 모든 property를 `required`에, nullable은 `["string","null"]`, `minItems`/`minimum` 등 미지원 키워드 금지
- role schema는 공통 skeleton 필수: `status` enum = `["PASS","NOT_PASS","NEED_REPLAN","BLOCKED","INSUFFICIENT_CAPABILITY"]`, `summary` property, `required`에 `status`
- 리포트 HTML은 **JS 0**, 인라인 CSS만, 외부 요청 0. 모든 삽입값은 `html.escape`
- 테스트 실행: `PYTHONPATH=src python -m pytest tests/ -q` (worktree에서는 그 worktree의 `src`)
- 커밋 메시지는 한국어 한 줄 요약 + 필요 시 본문 (기존 로그 형식)

---

### Task 1: Role 확장 — enum · config · 번들 2종

**Files:**
- Modify: `src/devcrew/schema.py` (Role enum)
- Modify: `config/harness.yaml` (roleDefaults)
- Create: `roles/tutor/prompt.md`, `roles/tutor/output.schema.json`
- Create: `roles/tutor_verifier/prompt.md`, `roles/tutor_verifier/output.schema.json`
- Test: `tests/test_roles.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `Role.TUTOR`, `Role.TUTOR_VERIFIER`; `load_bundle(Role.TUTOR)` / `load_bundle(Role.TUTOR_VERIFIER)`가 각각 유효한 `RoleBundle`을 돌려준다
- Produces: `config/harness.yaml`의 `roleDefaults.TUTOR.tier == "HIGH_CAPABILITY"`, `roleDefaults.TUTOR_VERIFIER.tier == "CODEX_DEFAULT"`

- [ ] **Step 1: 실패 테스트 — 두 role 번들이 로드된다**

```python
def test_tutor_bundles_load():
    from devcrew.roles import load_bundle
    from devcrew.schema import Role
    for role in (Role.TUTOR, Role.TUTOR_VERIFIER):
        b = load_bundle(role)
        assert b.prompt and b.schema["properties"]["status"]["enum"][0] == "PASS"

def test_tutor_output_schema_shape():
    from devcrew.roles import load_bundle
    from devcrew.schema import Role
    q = load_bundle(Role.TUTOR).schema["properties"]["questions"]["items"]
    assert q["additionalProperties"] is False
    assert set(q["required"]) == set(q["properties"])   # strict: 전부 required
    for key in ("area", "type", "stem", "options", "answer_index",
                "evidence", "explanation", "diagram"):
        assert key in q["properties"]

def test_tutor_roles_have_config_defaults():
    from devcrew.config import load
    from devcrew.schema import Role
    cfg = load()
    assert cfg.role_defaults[Role.TUTOR].tier == "HIGH_CAPABILITY"
    assert cfg.role_defaults[Role.TUTOR_VERIFIER].tier == "CODEX_DEFAULT"
```

- [ ] **Step 2: 실패 확인** — `Role.TUTOR` AttributeError 기대
- [ ] **Step 3: 구현** — Role enum 2개 추가, `harness.yaml` roleDefaults 2줄, 번들 4개 파일 작성

`roles/tutor/output.schema.json` 형태:

```json
{"type":"object","additionalProperties":false,
 "required":["status","summary","questions"],
 "properties":{
  "status":{"type":"string","enum":["PASS","NOT_PASS","NEED_REPLAN","BLOCKED","INSUFFICIENT_CAPABILITY"]},
  "summary":{"type":"string"},
  "questions":{"type":"array","items":{
    "type":"object","additionalProperties":false,
    "required":["area","type","stem","options","answer_index","evidence","explanation","diagram"],
    "properties":{
      "area":{"type":"string"},
      "type":{"type":"string","enum":["CORRECT","INCORRECT"]},
      "stem":{"type":"string"},
      "options":{"type":"array","items":{"type":"string"}},
      "answer_index":{"type":"integer"},
      "evidence":{"type":"array","items":{
        "type":"object","additionalProperties":false,
        "required":["path","start_line","end_line","quote"],
        "properties":{"path":{"type":"string"},"start_line":{"type":"integer"},
                      "end_line":{"type":"integer"},"quote":{"type":"string"}}}},
      "explanation":{"type":"string"},
      "diagram":{"type":["object","null"],"additionalProperties":true}}}}}}
```

`roles/tutor_verifier/output.schema.json`: `verdicts` 배열 — `{index:int, verdict:"PASS"|"REJECT", reason:string}`.

프롬프트 요지(두 개 모두): 근거 없는 출제 금지, 일반 지식 기반 문항 금지, 검증자는 repo를 직접 읽어 스스로 판정.

- [ ] **Step 4: 통과 확인** — `pytest tests/test_roles.py tests/test_config.py -q`
- [ ] **Step 5: 커밋** — `feat(tutor): TUTOR·TUTOR_VERIFIER role 번들과 config 기본값`

---

### Task 2: `quiz.py` — 문항 모델과 `q_key`

**Files:**
- Create: `src/devcrew/quiz.py`
- Test: `tests/test_quiz.py`

**Interfaces:**
- Produces: `Evidence(path, start_line, end_line, quote)`, `Question(area, type, stem, options, answer_index, evidence, explanation, diagram, key)`
- Produces: `parse_questions(raw: list[dict]) -> list[Question]` — 형태가 어긋난 항목은 조용히 버린다
- Produces: `q_key(area: str, evidence: list[Evidence]) -> str` — 12자 hex
- Produces: `Question.key`는 재출제 이월 키(`carried_key`)가 있으면 그것, 없으면 `q_key(...)`

- [ ] **Step 1: 실패 테스트**

```python
def test_q_key_ignores_stem_but_follows_evidence():
    from devcrew.quiz import Evidence, q_key
    e1 = [Evidence("src/a.py", 10, 20, "x")]
    e2 = [Evidence("src/a.py", 10, 25, "y")]      # 같은 시작 줄 = 같은 지점
    e3 = [Evidence("src/b.py", 10, 20, "x")]
    assert q_key("세션", e1) == q_key("세션", e2)
    assert q_key("세션", e1) != q_key("세션", e3)
    assert q_key("세션", e1) != q_key("리포트", e1)

def test_carried_key_survives_reissue():
    """재출제 문항은 원래 키를 유지한다 — 인용 줄이 흔들려도 오답이 해소된다."""
    from devcrew.quiz import parse_questions
    raw = [_q(area="세션", path="src/a.py", start=38)]
    q = parse_questions(raw, carried_keys={0: "deadbeef1234"})[0]
    assert q.key == "deadbeef1234"

def test_parse_questions_drops_malformed():
    from devcrew.quiz import parse_questions
    assert parse_questions([{"area": "x"}]) == []            # 필드 누락
    assert parse_questions([_q(options=["A", "B"])]) == []    # 보기 4개 아님
    assert parse_questions([_q(answer_index=9)]) == []        # 범위 밖
```

- [ ] **Step 2: 실패 확인** — ImportError 기대
- [ ] **Step 3: 구현** — dataclass 3개 + `parse_questions` + `q_key`(sha256 12자)
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** — `feat(tutor): 문항 모델과 재출제 이월 q_key`

---

### Task 3: `quiz.py` — 인용 대조 (결정적 검증)

**Files:**
- Modify: `src/devcrew/quiz.py`
- Test: `tests/test_quiz.py`

**Interfaces:**
- Produces: `verify_citations(questions: list[Question], root: str | Path) -> tuple[list[Question], list[tuple[Question, str]]]` — `(통과, [(문항, 폐기사유)])`

- [ ] **Step 1: 실패 테스트** — 실제 임시 파일에 대고

```python
def test_citation_must_exist_in_cited_lines(tmp_path):
    (tmp_path / "a.py").write_text("l1\nl2\nSESSION_TTL = 3600\nl4\n")
    from devcrew.quiz import verify_citations, parse_questions
    ok, dropped = verify_citations(parse_questions([
        _q(path="a.py", start=3, end=3, quote="SESSION_TTL = 3600"),
        _q(path="a.py", start=1, end=2, quote="SESSION_TTL = 3600"),   # 범위 밖
        _q(path="nope.py", start=1, end=1, quote="x"),                 # 없는 파일
        _q(path="../etc/passwd", start=1, end=1, quote="root"),        # 탈출
        _q(evidence=[]),                                               # 근거 없음
    ]), tmp_path)
    assert len(ok) == 1 and len(dropped) == 4
    assert {r for _, r in dropped} >= {"quote-not-in-range", "path-missing",
                                       "path-outside-repo", "no-evidence"}

def test_citation_match_normalizes_whitespace(tmp_path):
    (tmp_path / "a.py").write_text("def  f(x):\n    return   x\n")
    from devcrew.quiz import verify_citations, parse_questions
    ok, _ = verify_citations(parse_questions([
        _q(path="a.py", start=1, end=2, quote="def f(x): return x")]), tmp_path)
    assert len(ok) == 1
```

- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현** — `Path(root).resolve()` 기준 `is_relative_to` 로 탈출 차단, 줄 범위 슬라이스, `re.sub(r"\s+", " ", ...)` 정규화 후 부분 문자열
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** — `feat(tutor): 인용 대조 — 지어낸 근거를 LLM 없이 폐기`

---

### Task 4: `quiz.py` — 채점

**Files:**
- Modify: `src/devcrew/quiz.py`
- Test: `tests/test_quiz.py`

**Interfaces:**
- Produces: `Scorecard(total, correct, by_area: dict[str, tuple[int, int]], results: list[Result])`
- Produces: `Result(question, choice: int | None, correct: bool)`
- Produces: `grade(questions, answers: dict[int, int]) -> Scorecard` — 미응답은 `choice=None`, 오답 취급

- [ ] **Step 1: 실패 테스트**

```python
def test_grade_counts_by_area_and_treats_unanswered_as_wrong():
    from devcrew.quiz import grade, parse_questions
    qs = parse_questions([_q(area="세션", answer_index=0),
                          _q(area="세션", answer_index=1),
                          _q(area="리포트", answer_index=2)])
    card = grade(qs, {0: 0, 1: 3})            # 2번 문항 미응답
    assert (card.correct, card.total) == (1, 3)
    assert card.by_area == {"세션": (1, 2), "리포트": (0, 1)}
    assert card.results[2].choice is None and card.results[2].correct is False
```

- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** — `feat(tutor): 채점과 영역별 집계`

---

### Task 5: `quiz.py` — 오답 노트 (trace 기반)

**Files:**
- Modify: `src/devcrew/quiz.py`
- Test: `tests/test_quiz.py`

**Interfaces:**
- Produces: `note_id(user: str, repo: str) -> str` → `f"TUTOR-{user}-{repo}"`
- Produces: `open_misses(trace, exec_id) -> list[dict]` — `QuizMissEvent` 중 뒤에 같은 `q_key`의 `QuizClearedEvent`가 **없는** 것 (rowid 순)
- Produces: `record_scorecard(trace, exec_id, card, prior_keys: set[str]) -> tuple[int, int]` — `(새 오답 수, 해소 수)`. 오답이면 `QuizMissEvent`, `prior_keys`에 있던 문항을 맞히면 `QuizClearedEvent`

- [ ] **Step 1: 실패 테스트**

```python
def test_miss_clear_miss_leaves_the_miss_open(tmp_path):
    """해소 후 다시 틀리면 다시 열린다 — 벽시계가 아니라 rowid 순서로 판정한다."""
    from devcrew.store.trace import TraceStore
    from devcrew.quiz import open_misses
    t = TraceStore(tmp_path / "t.db")
    for et in ("QuizMissEvent", "QuizClearedEvent", "QuizMissEvent"):
        t.append(et, task_id="T", execution_id="TUTOR-U1-repo",
                 payload={"q_key": "k1", "area": "세션"})
    assert [m["q_key"] for m in open_misses(t, "TUTOR-U1-repo")] == ["k1"]

def test_cleared_after_miss_removes_it(tmp_path):
    from devcrew.store.trace import TraceStore
    from devcrew.quiz import open_misses
    t = TraceStore(tmp_path / "t.db")
    t.append("QuizMissEvent", task_id="T", execution_id="E", payload={"q_key": "k1"})
    t.append("QuizMissEvent", task_id="T", execution_id="E", payload={"q_key": "k2"})
    t.append("QuizClearedEvent", task_id="T", execution_id="E", payload={"q_key": "k1"})
    assert [m["q_key"] for m in open_misses(t, "E")] == ["k2"]
```

- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현** — `trace.events(execution_id=...)`를 한 번 읽어 id 순으로 접으며 열림/해소 판정
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** — `feat(tutor): 오답 노트 — trace 이벤트로 열림·해소 판정`

---

### Task 6: `report/charts.py` — 도식 스펙 → 인라인 SVG

**Files:**
- Create: `src/devcrew/report/charts.py`
- Test: `tests/test_charts.py`

**Interfaces:**
- Produces: `render_diagram(spec: dict | None) -> str` — HTML 조각. `None`/알 수 없는 타입/렌더 실패는 **표 폴백**, 그마저 불가하면 빈 문자열
- Produces: `bar_chart(items: list[tuple[str, float]], *, unit: str = "") -> str` — 요약용(영역별 정답률)

- [ ] **Step 1: 실패 테스트**

```python
def test_bar_chart_is_inline_svg_without_script():
    from devcrew.report.charts import bar_chart
    out = bar_chart([("세션", 80.0), ("리포트", 50.0)], unit="%")
    assert out.startswith("<svg") and "<script" not in out
    assert "세션" in out and "80" in out

def test_unknown_diagram_type_falls_back_to_table():
    from devcrew.report.charts import render_diagram
    out = render_diagram({"type": "sankey", "items": [{"label": "a", "value": 1}]})
    assert "<table" in out and "<svg" not in out

def test_diagram_escapes_labels():
    from devcrew.report.charts import render_diagram
    out = render_diagram({"type": "bar", "items": [{"label": "<script>x</script>", "value": 1}]})
    assert "<script>" not in out and "&lt;script&gt;" in out

def test_none_diagram_renders_nothing():
    from devcrew.report.charts import render_diagram
    assert render_diagram(None) == ""
```

- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현** — `bar`(가로 막대 rect+text), `flow`(노드 상자 + 화살표 line), 그 외 표 폴백. 전 출력 `html.escape`
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** — `feat(tutor): 도식 스펙 → 인라인 SVG (미지원은 표 폴백)`

---

### Task 7: `report/quiz_report.py` — 영역별 카드 리포트

**Files:**
- Create: `src/devcrew/report/quiz_report.py`
- Test: `tests/test_quiz_report.py`

**Interfaces:**
- Consumes: `Scorecard`(Task 4), `render_diagram`/`bar_chart`(Task 6)
- Produces: `render_quiz_report(card: Scorecard, *, repo: str | None, added: int, cleared: int) -> str`

- [ ] **Step 1: 실패 테스트**

```python
def test_report_groups_cards_by_area_and_hides_explanation_behind_details():
    html = render_quiz_report(card, repo="message-gate", added=2, cleared=1)
    assert "<script" not in html and "http://" not in html
    assert html.count("<details") == card.total          # 문항당 하나
    assert "<h2>세션</h2>" in html                        # 영역 그룹 헤더
    assert "message-gate" in html

def test_report_escapes_question_text():
    # 지문에 <img onerror=...> 를 넣고 그대로 나오지 않는지
    assert "<img" not in html and "&lt;img" in html

def test_report_shows_note_delta():
    assert "오답 +2" in html and "해소 1" in html
```

- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현** — 인라인 CSS, 상단 요약(총점 + `bar_chart(영역별 정답률)` + 오답 노트 증감), 영역별 `<h2>`, 문항 카드 `<details><summary>지문 + 정오</summary>` 안에 정답·해설·`render_diagram`·근거 `path:line`
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** — `feat(tutor): 영역별 카드 리포트 (JS 0, details 펼침)`

---

### Task 8: `tutor.py` — 출제 파이프라인

**Files:**
- Create: `src/devcrew/tutor.py`
- Test: `tests/test_tutor_pipeline.py`

**Interfaces:**
- Consumes: `parse_questions`, `verify_citations`, `q_key`, `open_misses`
- Produces: `async issue_quiz(orch, cfg, *, repo_name, repo_path, exec_id, misses) -> IssueResult`
- Produces: `IssueResult(questions: list[Question], shortfall: bool, notes: list[str])`
- Produces: `select_ten(passed: list[Question], carried: set[str]) -> list[Question]` — ①오답 유래(최대 3) ②area 미중복 ③출제 순서

- [ ] **Step 1: 실패 테스트 (FakeAdapter)**

```python
async def test_pipeline_drops_fabricated_then_verifies_then_selects_ten(tmp_path):
    """12문항 → 인용 대조 → Codex 검증 → 10문항. 각 관문이 실제로 깎는지."""
    # 출제 12개 중 2개는 없는 파일 인용, 검증자가 1개 REJECT → 9개 → 보충 1회
    ...
    assert len(res.questions) == 10
    assert verifier_spawned_with_provider == Provider.CODEX

async def test_pipeline_reports_shortfall_instead_of_padding():
    """보충 후에도 미달이면 채운 만큼만, shortfall=True."""
    assert len(res.questions) == 6 and res.shortfall is True

def test_select_ten_prioritizes_carried_then_area_diversity():
    picked = select_ten(passed, carried={"k1", "k2"})
    assert [q.key for q in picked[:2]] == ["k1", "k2"]
    assert len({q.area for q in picked}) >= 4

async def test_verifier_gets_questions_but_not_authoring_transcript():
    """검증자 투입 메시지에 출제 세션의 논증이 섞이면 교차 검증이 아니다."""
    assert "출제 근거를 다시 설명" not in verifier_input
    assert "evidence" in verifier_input
```

- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현** — `orch.spawn(Role.TUTOR, tier, worktree=repo_path)` → structured 출제 → `verify_citations` → `orch.spawn(Role.TUTOR_VERIFIER, ...)` 별도 인스턴스 → 판정 반영 → `select_ten` → 미달 시 보충 1회
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** — `feat(tutor): 출제 파이프라인 — 대조·Codex 교차 검증·선별`

---

### Task 9: `slack_tutor.py` — 세션과 문항 진행

**Files:**
- Create: `src/devcrew/slack_tutor.py`
- Test: `tests/test_slack_tutor.py`

**Interfaces:**
- Consumes: Task 8의 `issue_quiz`, Task 4의 `grade`, Task 5의 `record_scorecard`, Task 7의 `render_quiz_report`
- Produces: `TutorHandler(orch, cfg, repos, *, react=None, status=None, publish=publish_report)`
- Produces: `async on_mention(body, say)`, `async on_answer(*, thread_ts, value, say, strip=None, channel="", user="")`
- Produces: 버튼 `action_id = f"tutor_answer_{idx}_{choice}"`, `value = f"{idx}:{choice}"`

- [ ] **Step 1: 실패 테스트**

```python
async def test_mention_issues_quiz_and_posts_first_question(tmp_path):
    assert "1 / 10" in say.messages[-1]["text"]
    assert len(buttons_of(say.messages[-1])) == 4

async def test_answers_advance_and_do_not_reveal_correctness(tmp_path):
    await h.on_answer(thread_ts="100.1", value="0:2", say=say, user="U-OWNER")
    assert "정답" not in say.messages[-1]["text"] and "2 / 10" in say.messages[-1]["text"]

async def test_completing_ten_grades_and_publishes_report(tmp_path):
    assert published and "http" in say.messages[-1]["text"]

async def test_only_owner_can_answer(tmp_path):
    await h.on_answer(thread_ts="100.1", value="0:1", say=say, user="U-STRANGER")
    assert "시작한 사람만" in say.messages[-1]["text"]

async def test_lost_session_resumes_from_issued_event(tmp_path):
    """세션이 죽어도 QuizIssuedEvent + 답변 이벤트로 이어 푼다."""
    h.sessions.clear()
    await h.on_answer(thread_ts="100.1", value="3:0", say=say, user="U-OWNER")
    assert "5 / 10" in say.messages[-1]["text"]

async def test_stale_round_is_not_resumed(tmp_path):
    """24시간이 지난 미완 회차는 되살리지 않는다."""
    assert "다시 시작" in say.messages[-1]["text"]

async def test_shortfall_is_told_not_hidden(tmp_path):
    assert "근거를 찾지 못해" in say.messages[0]["text"] and "6문항" in say.messages[0]["text"]
```

- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현** — `slack_brain`의 세션 수명·`_dedupe`·`_may_command`·Block Kit 패턴을 따른다. 답변마다 `QuizAnswerEvent` append, 10문항 완료 시 `grade` → `record_scorecard` → `render_quiz_report` → `publish`
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** — `feat(tutor): Slack 퀴즈 세션 — 문항 진행·재개·채점 발행`

---

### Task 10: 배선과 문서

**Files:**
- Modify: `src/devcrew/slack_engine.py` (@tutor 앱 블록)
- Modify: `DESIGN.md` (TUTOR 절)
- Modify: `README.md` 또는 `docs/` 운영 메모 (`TUTOR_BOT_TOKEN`/`TUTOR_APP_TOKEN` 설정 안내)
- Test: `tests/test_slack_engine.py`

**Interfaces:**
- Consumes: `TutorHandler`
- Produces: `TUTOR_BOT_TOKEN`/`TUTOR_APP_TOKEN`이 있을 때만 `@tutor` 앱이 뜬다 (brain과 같은 게이팅)

- [ ] **Step 1: 실패 테스트** — 토큰 없으면 tutor 앱을 만들지 않는다 (기존 engine 테스트 형식에 맞춰)
- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현** — `brain_app` 블록과 같은 형태로 `tutor_app` 추가, `@tutor_app.action(re.compile("tutor_answer_.*"))` 배선
- [ ] **Step 4: 전체 스위트 통과 확인** — `PYTHONPATH=src python -m pytest tests/ -q`
- [ ] **Step 5: 커밋** — `feat(tutor): @tutor Slack 앱 배선과 운영 문서`

---

## 완료 기준 (사용자 테스트 시점)

1. `PYTHONPATH=src python -m pytest tests/ -q` 전부 통과
2. 로컬 POC 스크립트로 실제 repo에 대해 출제→검증→채점→HTML 생성이 도는 것을 확인 (`poc/` 관례를 따른다)
3. Slack 앱 토큰을 사용자가 발급한 뒤 `@tutor <repo>:` 로 실제 회차 1개 완주
