"""slack_tutor — 퀴즈 세션·문항 진행·재개·채점 발행 (FakeAdapter, Slack 없음)."""
import asyncio


import time

import pytest

from devcrew.adapters.base import FakeAdapter
from devcrew.config import load as load_config
from devcrew.orchestrator import Orchestrator
from devcrew.schema import Provider
from devcrew.store.registry import SessionRegistry
from devcrew.store.trace import TraceStore


class Scripted(FakeAdapter):
    def __init__(self, structured):
        super().__init__(script=["ok"] * 50)
        self.queue = list(structured)

    def _next_structured(self, session_id, n):
        # `send`가 아니라 이 훅을 덮는다 — `send`를 덮으면 "스키마 없는 세션은 구조화
        # 출력을 내지 않는다"는 실 어댑터 계약까지 함께 우회한다 (base.py 참조).
        return self.queue.pop(0) if self.queue else super()._next_structured(session_id, n)


class SaySpy:
    def __init__(self):
        self.messages = []
        self._n = 0

    async def __call__(self, *, text, thread_ts=None, blocks=None):
        self.messages.append({"text": text, "thread_ts": thread_ts, "blocks": blocks})
        # 실제 `chat_postMessage`처럼 응답을 돌려준다 — 진행 표시가 여기서 `ts`를 얻는다
        self._n += 1
        return {"ok": True, "ts": f"msg-{self._n}"}


def buttons_of(msg):
    for b in msg.get("blocks") or []:
        if b["type"] == "actions":
            return b["elements"]
    return []


def _q(i, area=None, start=1):
    return {"area": area or f"영역{i % 4}", "type": "CORRECT", "stem": f"문항 {i}?",
            "options": ["A", "B", "C", "D"], "answer_index": 0,
            "evidence": [{"path": "a.py", "start_line": start, "end_line": start,
                          "quote": f"line{start}"}],
            "explanation": f"해설 {i}", "diagram": None, "source_key": None}


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    (d / "a.py").write_text("\n".join(f"line{i}" for i in range(1, 40)) + "\n")
    return d


def make_handler(tmp_path, repo, *, n=12, published=None, ta_answers=3):
    trace = TraceStore(tmp_path / "t.db")
    # 첫 항목 뒤로는 TUTOR_TA(후속 질문)용 — 이 role도 tier DEFAULT(CLAUDE_CODE)라 같은
    # author adapter를 쓴다. 출제가 첫 send()에서 첫 항목을 소비하므로, 후속 질문의
    # send()들은 이어지는 항목을 순서대로 받는다. `ta_answers`만큼 질문을 이어 물을 수
    # 있다 — 세션 재사용(두 번째 질문부터 같은 세션)을 테스트하려면 2개 이상 필요하다.
    author = Scripted([{"status": "PASS", "summary": "s",
                        "questions": [_q(i, start=i + 1) for i in range(n)]}] +
                      [{"answer": f"후속 질문에 대한 답변 {i + 1}.", "citations": []}
                       for i in range(ta_answers)])
    verifier = Scripted([{"status": "PASS", "summary": "v",
                          "verdicts": [{"index": i, "verdict": "PASS", "reason": "ok"}
                                       for i in range(n)]}])
    orch = Orchestrator(trace, SessionRegistry(tmp_path / "h.db"),
                        {Provider.CLAUDE_CODE: author, Provider.CODEX: verifier})
    from devcrew.slack_tutor import TutorHandler
    pub = published if published is not None else []

    def publish(task_id, html):
        pub.append((task_id, html))
        return f"https://reports.example/{task_id}"

    h = TutorHandler(orch, load_config(), {"myrepo": repo}, publish=publish,
                     corpus_root=tmp_path / "corpus")
    return h, trace, pub


def mention(text="<@U1> myrepo:", ts="100.1", user="U-OWNER", event_id="Ev1"):
    return {"event_id": event_id,
            "event": {"text": text, "ts": ts, "channel": "C1", "user": user}}


async def answer_all(h, say, *, correct=10, total=10, user="U-OWNER"):
    for i in range(total):
        await h.on_answer(thread_ts="100.1", value=f"{i}:{0 if i < correct else 1}",
                          say=say, user=user, channel="C1")


@pytest.mark.asyncio
async def test_mention_issues_quiz_and_posts_first_question(tmp_path, repo):
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    assert "1 / 10" in say.messages[-1]["text"]
    assert len(buttons_of(say.messages[-1])) == 4


@pytest.mark.asyncio
async def test_answers_advance_without_revealing_correctness(tmp_path, repo):
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await h.on_answer(thread_ts="100.1", value="0:2", say=say, user="U-OWNER", channel="C1")
    assert "2 / 10" in say.messages[-1]["text"]
    assert "정답" not in say.messages[-1]["text"] and "오답" not in say.messages[-1]["text"]


@pytest.mark.asyncio
async def test_completing_the_round_grades_and_publishes_report(tmp_path, repo):
    h, trace, pub = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say, correct=7)
    assert len(pub) == 1 and "<details" in pub[0][1]
    assert "7 / 10" in say.messages[-1]["text"]
    assert "https://reports.example/" in say.messages[-1]["text"]


@pytest.mark.asyncio
async def test_wrong_answers_land_in_the_miss_note_and_are_cleared_next_time(tmp_path, repo):
    """오답 노트가 회차를 건너 살아남고, 다시 맞히면 지워진다."""
    from devcrew.quiz import note_id, open_misses
    h, trace, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say, correct=8)                 # 2문항 오답
    note = note_id("U-OWNER", "myrepo")
    assert len(open_misses(trace, note)) == 2
    keys = {m["q_key"] for m in open_misses(trace, note)}
    # 같은 문항을 다음 회차에서 전부 맞히면 노트가 비워진다
    from devcrew.quiz import Scorecard, from_raw, grade, record_scorecard
    qs = h.sessions["100.1"].questions if "100.1" in h.sessions else None
    assert qs is None or True
    card = grade([q for q in h.last_questions if q.key in keys],
                 {i: 0 for i in range(len(keys))})
    added, cleared = record_scorecard(trace, note, card, prior_keys=keys)
    assert (added, cleared) == (0, 2) and open_misses(trace, note) == []


@pytest.mark.asyncio
async def test_only_the_owner_can_answer(tmp_path, repo):
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await h.on_answer(thread_ts="100.1", value="0:0", say=say, user="U-STRANGER",
                      channel="C1")
    assert "시작한 사람만" in say.messages[-1]["text"]
    assert h.sessions["100.1"].answers == {}


@pytest.mark.asyncio
async def test_lost_session_resumes_from_trace(tmp_path, repo):
    """세션이 죽어도 QuizIssuedEvent + 답변 이벤트로 이어 푼다 (append-only가 진실)."""
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    for i in range(4):
        await h.on_answer(thread_ts="100.1", value=f"{i}:0", say=say, user="U-OWNER",
                          channel="C1")
    h.sessions.clear()                                   # 프로세스 재시작
    await h.on_answer(thread_ts="100.1", value="4:0", say=say, user="U-OWNER", channel="C1")
    assert "6 / 10" in say.messages[-1]["text"]


@pytest.mark.asyncio
async def test_stale_round_is_not_resumed(tmp_path, repo, monkeypatch):
    """하루가 지난 미완 회차는 되살리지 않는다 (trace는 append-only라 시계를 옮긴다)."""
    import time as _time
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    h.sessions.clear()
    later = _time.time() + 30 * 3600
    monkeypatch.setattr("devcrew.slack_tutor.time.time", lambda: later)
    await h.on_answer(thread_ts="100.1", value="0:0", say=say, user="U-OWNER", channel="C1")
    assert "다시 시작" in say.messages[-1]["text"]


@pytest.mark.asyncio
async def test_bare_mention_explains_both_modes(tmp_path, repo):
    """멘션만 하면 두 모드를 다 알려준다 — 접두 없는 문장은 이제 오류가 아니라
    주제 학습이므로(§10.8), "repo를 지정하라"만 말하면 거짓 안내가 된다."""
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(text="<@U1>   "), say)
    text = say.messages[-1]["text"]
    assert "주제" in text and "repo" in text
    assert h.sessions == {}


@pytest.mark.asyncio
async def test_shortfall_is_told_not_hidden(tmp_path, repo):
    """근거를 못 찾아 문항이 모자라면 숨기지 않고 알린다."""
    h, _, _ = make_handler(tmp_path, repo, n=6)
    say = SaySpy()
    await h.on_mention(mention(), say)
    assert any("6문항" in m["text"] for m in say.messages)
    assert "1 / 6" in say.messages[-1]["text"]


@pytest.mark.asyncio
async def test_duplicate_event_is_ignored(tmp_path, repo):
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    n = len(say.messages)
    await h.on_mention(mention(), say)                   # 같은 event_id
    assert len(say.messages) == n


# ── Codex 리뷰 대응 (#19) ───────────────────────────────────────────────────
class FlakyTrace:
    """N번째 append부터 실패하는 trace 래퍼 — 기록 실패 시 UI가 앞서가는지 본다."""

    def __init__(self, inner, fail_on: str):
        self._inner = inner
        self._fail_on = fail_on

    def append(self, event_type, **kw):
        if event_type == self._fail_on:
            raise RuntimeError("trace down")
        return self._inner.append(event_type, **kw)

    def events(self, **kw):
        return self._inner.events(**kw)

    def __getattr__(self, name):        # 나머지는 그대로 위임 — 대역 구멍이 결함을 가린다
        return getattr(self._inner, name)


@pytest.mark.asyncio
async def test_issue_is_recorded_before_the_round_is_announced(tmp_path, repo):
    """기록이 먼저다 — 안내를 먼저 보내면 기록 실패 시 사용자에게 죽은 회차가 남는다."""
    h, trace, _ = make_handler(tmp_path, repo)
    h.orch.trace = FlakyTrace(trace, "QuizIssuedEvent")
    say = SaySpy()
    await h.on_mention(mention(), say)
    assert "100.1" not in h.sessions
    assert "실패" in say.messages[-1]["text"]
    assert not any("1 / 10" in m["text"] for m in say.messages)   # 문항을 안 띄운다


@pytest.mark.asyncio
async def test_answer_is_recorded_before_the_button_is_stripped(tmp_path, repo):
    """버튼을 먼저 걷으면 기록 실패 시 답을 다시 낼 방법이 없다."""
    h, trace, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    h.orch.trace = FlakyTrace(trace, "QuizAnswerEvent")
    stripped = []

    async def strip():
        stripped.append(True)

    await h.on_answer(thread_ts="100.1", value="0:1", say=say, strip=strip,
                      user="U-OWNER", channel="C1")
    assert stripped == []                                  # 버튼은 그대로 남는다
    assert h.sessions["100.1"].answers == {}                # 답도 안 들어간다
    assert "실패" in say.messages[-1]["text"]


@pytest.mark.asyncio
async def test_round_aborts_when_the_miss_note_cannot_be_read(tmp_path, repo):
    """노트를 못 읽으면 회차를 열지 않는다 — 열면 기존 오답이 해소되지 않는다."""
    h, trace, _ = make_handler(tmp_path, repo)

    class BrokenTrace(FlakyTrace):
        def events(self, **kw):
            raise RuntimeError("db locked")

    h.orch.trace = BrokenTrace(trace, "__never__")
    say = SaySpy()
    await h.on_mention(mention(), say)
    assert h.sessions == {}
    assert "오답 노트" in say.messages[-1]["text"]


@pytest.mark.asyncio
async def test_grading_failure_leaves_the_round_retryable(tmp_path, repo):
    """채점 기록이 실패했는데 done을 세우면 회차가 영구히 채점 불가가 된다.
    버튼도 이미 걷힌 뒤라 사용자에게는 되살릴 손잡이가 없다."""
    from devcrew.slack_tutor import REGRADE_VALUE
    h, trace, pub = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    h.orch.trace = FlakyTrace(trace, "QuizGradedEvent")
    await answer_all(h, say, correct=8)                   # 2문항 오답 → miss 기록 실패
    assert h.sessions["100.1"].done is False              # 재시도 가능해야 한다
    assert pub == []
    btns = buttons_of(say.messages[-1])                   # 다시 채점할 손잡이를 준다
    assert [b["value"] for b in btns] == [REGRADE_VALUE]

    h.orch.trace = trace                                  # 복구된 뒤 다시 채점
    await h.on_answer(thread_ts="100.1", value=REGRADE_VALUE, say=say,
                      user="U-OWNER", channel="C1")
    assert len(pub) == 1 and "8 / 10" in say.messages[-1]["text"]


@pytest.mark.asyncio
async def test_regrade_is_owner_only(tmp_path, repo):
    from devcrew.slack_tutor import REGRADE_VALUE
    h, trace, pub = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    h.orch.trace = FlakyTrace(trace, "QuizGradedEvent")
    await answer_all(h, say, correct=8)
    await h.on_answer(thread_ts="100.1", value=REGRADE_VALUE, say=say,
                      user="U-STRANGER", channel="C1")
    assert pub == [] and "시작한 사람만" in say.messages[-1]["text"]


# ── Slack 렌더링 (사용자 피드백 2026-08-21: "마크다운이 그대로 보인다") ─────────
def _question(**kw):
    from devcrew.quiz import Evidence, Question
    base = dict(area="세션 수명", type="INCORRECT", stem="다음 중 **틀린** 것은?",
                options=[f"보기 {c}" for c in "ABCD"], answer_index=0,
                evidence=[Evidence("a.py", 1, 1, "line1")], explanation="해설")
    return Question(**{**base, **kw})


def _sections(blocks):
    return [b["text"]["text"] for b in blocks if b["type"] == "section"]


def test_question_blocks_convert_markdown_to_slack_mrkdwn():
    """Slack은 `**bold**`를 굵게 그리지 않는다 — 안 바꾸면 별표가 날문자로 보인다."""
    from devcrew.slack_tutor import question_blocks
    blocks = question_blocks(_question(), 0, 10)
    stem = _sections(blocks)[0]
    assert "**" not in stem and "*틀린*" in stem


def test_question_blocks_give_each_option_its_own_section():
    """보기 넷을 한 덩어리에 넣으면 긴 보기끼리 붙어 A·B·C·D 경계가 사라진다."""
    from devcrew.slack_tutor import question_blocks
    opts = ["매우 긴 보기 " * 12 + c for c in "ABCD"]
    blocks = question_blocks(_question(options=opts), 2, 10)
    labelled = [s for s in _sections(blocks) if s.startswith(("*A*", "*B*", "*C*", "*D*"))]
    assert len(labelled) == 4


def test_question_blocks_escape_angle_brackets():
    """`<...>`를 안 걷으면 Slack이 링크 문법으로 먹어 보기가 통째로 사라진다."""
    from devcrew.slack_tutor import question_blocks
    blocks = question_blocks(_question(options=["Callable[<T>]", "b", "c", "d"]), 0, 4)
    assert any("&lt;T&gt;" in s for s in _sections(blocks))


def test_header_carries_no_raw_markdown_marks():
    """header는 plain_text라 마크다운 기호가 그대로 찍힌다."""
    from devcrew.slack_tutor import question_blocks
    blocks = question_blocks(_question(area="`routing.py` **경계**"), 0, 4)
    head = next(b for b in blocks if b["type"] == "header")["text"]["text"]
    assert "*" not in head and "`" not in head


def test_score_blocks_break_areas_into_lines_with_a_report_button():
    """영역별을 한 줄로 이어 붙이면 어디가 약한지가 안 보인다."""
    from devcrew.quiz import Scorecard
    from devcrew.slack_tutor import score_blocks
    card = Scorecard(total=4, correct=2, by_area={"세션": (1, 2), "라우팅": (1, 2)})
    blocks = score_blocks(card, repo_name="myrepo", url="https://r/x", added=2, cleared=0)
    areas = next(s for s in _sections(blocks) if "영역별" in s)
    assert areas.count("\n") >= 2
    btn = next(b for b in blocks if b["type"] == "actions")["elements"][0]
    assert btn["url"] == "https://r/x"


# ── Block Kit 한도 (2026-08-21) ─────────────────────────────────────────────
# Slack이 거절하면 문항이 아예 안 뜬다. 길이는 모델이 정하므로 상한을 테스트로 고정한다.
LIMITS = {"blocks": 50, "section": 3000, "header": 150, "context_el": 10,
          "actions_el": 25, "button": 75, "value": 2000}


def assert_block_kit_valid(blocks):
    assert len(blocks) <= LIMITS["blocks"], f"블록 {len(blocks)}개"
    for b in blocks:
        if b["type"] == "section":
            assert len(b["text"]["text"]) <= LIMITS["section"]
            assert b["text"]["type"] == "mrkdwn"
        elif b["type"] == "header":
            assert b["text"]["type"] == "plain_text"
            assert len(b["text"]["text"]) <= LIMITS["header"]
        elif b["type"] == "context":
            assert len(b["elements"]) <= LIMITS["context_el"]
        elif b["type"] == "actions":
            assert len(b["elements"]) <= LIMITS["actions_el"]
            for e in b["elements"]:
                assert e["text"]["type"] == "plain_text"
                assert 0 < len(e["text"]["text"]) <= LIMITS["button"]
                assert len(e.get("value", "")) <= LIMITS["value"]


def test_question_blocks_stay_inside_slack_limits_on_absurd_input():
    from devcrew.slack_tutor import question_blocks
    q = _question(area="영" * 400, stem="지" * 5000,
                  options=["보" * 3000, "`" + "코" * 200 + "`", "**" + "굵" * 100 + "**", "d"])
    assert_block_kit_valid(question_blocks(q, 0, 10))


def test_opening_and_score_blocks_stay_inside_slack_limits():
    from devcrew.quiz import Scorecard
    from devcrew.slack_tutor import opening_blocks, regrade_blocks, score_blocks
    assert_block_kit_valid(opening_blocks("리" * 300, 10, shortfall=True, misses=99))
    card = Scorecard(total=60, correct=31,
                     by_area={f"영역{i} " + "긴" * 50: (i % 3, 3) for i in range(30)})
    assert_block_kit_valid(score_blocks(card, repo_name="r" * 300,
                                        url="https://example.test/" + "x" * 200,
                                        added=9, cleared=9))
    assert_block_kit_valid(regrade_blocks())


def test_area_lines_are_cut_by_line_not_by_character():
    """글자 수로 자르면 마지막 줄이 문장 도중에 끊겨 고장난 것처럼 보인다."""
    from devcrew.quiz import Scorecard
    from devcrew.slack_tutor import AREA_LINES, score_blocks
    card = Scorecard(total=60, correct=30,
                     by_area={f"영역{i}": (1, 2) for i in range(AREA_LINES + 5)})
    text = next(b["text"]["text"] for b in score_blocks(
        card, repo_name="r", url=None, added=0, cleared=0)
        if b["type"] == "section" and "영역별" in b["text"]["text"])
    assert "외 5개 영역" in text                        # 자른 사실을 숨기지 않는다
    assert all(l.strip() for l in text.splitlines())    # 잘린 반쪽 줄이 없다


@pytest.mark.asyncio
async def test_second_round_says_it_is_waiting_instead_of_going_silent(tmp_path, repo):
    """출제는 한 번에 하나만 돈다. 말해 주지 않으면 기다리는 쪽에는 무반응으로 보이고,
    사용자는 멘션을 반복한다 (2026-08-24 실사고)."""
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h._lock.acquire()             # 다른 회차가 출제 중인 상태
    try:
        task = asyncio.create_task(h.on_mention(mention(ts="200.1", event_id="Ev2"), say))
        for _ in range(50):             # 안내가 나갈 때까지만 양보한다
            await asyncio.sleep(0)
            if say.messages:
                break
        assert say.messages, "대기 중이라는 안내가 없다"
        assert "출제하는 중" in say.messages[0]["text"]
    finally:
        h._lock.release()
    await asyncio.wait_for(task, timeout=10)
    assert "1 / 10" in say.messages[-1]["text"]      # 풀리면 이어서 시작한다


@pytest.mark.asyncio
async def test_question_during_an_open_round_is_refused_once(tmp_path, repo):
    """진행 중에는 정답을 공개하지 않는다 — 질문은 그 원칙을 우회하는 경로다."""
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    before = len(say.messages)

    await h.on_question(thread_ts="100.1", text="3번 답이 뭐야?", user="U-OWNER", say=say)
    assert "채점" in say.messages[-1]["text"]
    after_first = len(say.messages)

    await h.on_question(thread_ts="100.1", text="그래도 알려줘", user="U-OWNER", say=say)
    assert len(say.messages) == after_first     # 두 번째부터는 조용히 버린다
    assert after_first == before + 1


@pytest.mark.asyncio
async def test_question_after_grading_gets_an_answer(tmp_path, repo):
    """채점 리포트가 아니라 실제 TA 답변이 새로 나가야 한다 — no-op으로도 통과하면
    안 된다 (`_finish`가 이미 채점 리포트를 보낸 뒤라 메시지가 있다는 것만으로는
    `on_question`이 뭔가 했다는 증거가 안 된다)."""
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)                     # 10문항을 다 풀어 채점까지
    n = len(say.messages)
    await h.on_question(thread_ts="100.1", text="왜 그런가요?", user="U-OWNER", say=say)
    assert len(say.messages) == n + 1
    assert say.messages[-1]["thread_ts"] == "100.1"
    assert "후속 질문에 대한 답변" in say.messages[-1]["text"]


@pytest.mark.asyncio
async def test_only_the_round_owner_may_ask(tmp_path, repo):
    """답변에는 정답과 근거가 그대로 들어간다 — 남에게는 스포일러다."""
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)
    n = len(say.messages)
    await h.on_question(thread_ts="100.1", text="왜?", user="U-STRANGER", say=say)
    assert len(say.messages) == n + 1
    assert "시작한 사람" in say.messages[-1]["text"]


@pytest.mark.asyncio
async def test_question_on_an_unknown_thread_is_ignored(tmp_path, repo):
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_question(thread_ts="999.9", text="왜?", user="U-OWNER", say=say)
    assert say.messages == []


@pytest.mark.asyncio
async def test_answer_failure_is_reported_not_swallowed(tmp_path, repo, monkeypatch):
    """조용히 삼키면 사용자도 우리도 원인을 못 찾는다."""
    import devcrew.slack_tutor as st
    from devcrew.tutor_ta import TutorTAError

    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)

    async def boom(*a, **kw):
        raise TutorTAError("TimeoutError")

    monkeypatch.setattr(st, "ask", boom)
    await h.on_question(thread_ts="100.1", text="왜?", user="U-OWNER", say=say)
    assert "TimeoutError" in say.messages[-1]["text"]


@pytest.mark.asyncio
async def test_expired_round_refuses_questions(tmp_path, repo):
    """하루가 지나면 그때의 근거로 답하는 것이 오히려 틀린 설명이 된다."""
    import devcrew.slack_tutor as st

    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)
    h.sessions["100.1"].started_at -= st.ROUND_TTL + 1

    n = len(say.messages)
    await h.on_question(thread_ts="100.1", text="왜?", user="U-OWNER", say=say)
    assert len(say.messages) == n + 1
    assert "하루가 지나" in say.messages[-1]["text"]


@pytest.mark.asyncio
async def test_question_and_answer_are_recorded_in_trace(tmp_path, repo):
    """세션은 프로세스와 함께 사라지지만 trace는 남는다."""
    from devcrew.quiz import QUESTION_EVENT, TA_ANSWER_EVENT

    h, trace, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)
    await h.on_question(thread_ts="100.1", text="왜 그런가요?", user="U-OWNER", say=say)

    kinds = [e["event_type"] for e in trace.events(execution_id="QUIZ-100.1")]
    assert QUESTION_EVENT in kinds and TA_ANSWER_EVENT in kinds


@pytest.mark.asyncio
async def test_second_question_reuses_the_same_ta_session(tmp_path, repo):
    """이 기능의 목적 — 대화가 이어져야 앞 질문의 맥락 위에서 답한다. 매번 새 세션을
    열면 회차 맥락을 다시 준다 해도 방금 나눈 대화 자체는 잊는다."""
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)

    author = h.orch.adapters[Provider.CLAUDE_CODE]
    sessions_before = len(author.turns)      # 출제 때 이미 세션 하나를 씀

    await h.on_question(thread_ts="100.1", text="첫 번째 질문", user="U-OWNER", say=say)
    sid1, provider1 = h.sessions["100.1"].ta_session_id, h.sessions["100.1"].ta_provider
    sessions_after_first = len(author.turns)
    assert sessions_after_first == sessions_before + 1     # 새 세션 하나만 열렸다

    await h.on_question(thread_ts="100.1", text="두 번째 질문", user="U-OWNER", say=say)
    sid2, provider2 = h.sessions["100.1"].ta_session_id, h.sessions["100.1"].ta_provider

    assert sid1 is not None and sid1 == sid2
    assert provider1 == provider2
    assert len(author.turns) == sessions_after_first        # 두 번째는 세션을 새로 안 연다
    assert say.messages[-2]["text"] != say.messages[-1]["text"]   # 서로 다른 답변


@pytest.mark.asyncio
async def test_resume_does_not_confuse_another_rounds_grading_as_done(tmp_path, repo):
    """GRADED_EVENT(오답 노트)는 사용자·repo 단위 네임스페이스라 어느 회차의 채점인지
    특정하지 못한다. 이 회차를 방치한 채 같은 사용자가 같은 repo로 다른 회차를 채점해도,
    그 이벤트로 이 회차를 '끝난 것'으로 보면 아직 풀지 않은 문항의 정답·해설이 새는
    정답 유출이다 (리뷰 2026-08-24 재현). done은 이 회차 자신의 execution_id에 남는
    ROUND_GRADED_EVENT로만 판정해야 한다."""
    from devcrew.quiz import GRADED_EVENT, note_id

    h, trace, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)             # 회차 A(100.1) — 방치, 채점 안 됨

    # "다른 회차"가 채점됐다고 가정한다 — GRADED_EVENT는 회차를 구분하지 않는
    # note_id(사용자·repo) 네임스페이스에 쌓이므로, 이 회차의 것과 구분할 수 없다.
    trace.append(GRADED_EVENT, task_id="OTHER-ROUND",
                execution_id=note_id("U-OWNER", "myrepo"),
                payload={"missed": [], "cleared": []})

    h.sessions.clear()                             # 프로세스 재시작
    n = len(say.messages)
    await h.on_question(thread_ts="100.1", text="3번 답이 뭐야?", user="U-OWNER", say=say)

    assert h.sessions["100.1"].done is False
    assert len(say.messages) == n + 1
    assert "채점" in say.messages[-1]["text"]        # 여전히 NEED_GRADED — 유출 없음


@pytest.mark.asyncio
async def test_resume_restores_done_for_the_same_graded_round(tmp_path, repo):
    """방치된 다른 회차와 달리, 실제로 채점을 마친 이 회차는 재시작 뒤에도 후속
    질문을 받아야 한다 — fail-closed가 항상 거절로 이어지면 그것도 결함이다."""
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)                        # 채점까지 완료
    assert h.sessions["100.1"].done is True

    h.sessions.clear()                              # 프로세스 재시작
    n = len(say.messages)
    await h.on_question(thread_ts="100.1", text="왜 그런가요?", user="U-OWNER", say=say)

    assert h.sessions["100.1"].done is True
    assert len(say.messages) == n + 1
    assert "채점" not in say.messages[-1]["text"]    # 거절이 아니라 실제 답변


# --- 최종 리뷰 fix (2026-08-24): TA 세션 반납 (C3) + 만료 안내 1회 게이트 (I3) ---


async def ask_once(h, say, *, thread="100.1", text="왜?", user="U-OWNER"):
    await h.on_question(thread_ts=thread, text=text, user=user, say=say, channel="C1")


@pytest.mark.asyncio
async def test_expired_round_reclaims_the_ta_session(tmp_path, repo):
    """"이 회차는 못 쓴다"고 말하는 바로 그 자리가 그 회차의 워커를 반납할 마지막
    자리다. 거절만 하고 살려 두면 회수 경로가 아예 없는 것과 같다 (최종 리뷰 C3)."""
    import devcrew.slack_tutor as st

    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)
    await ask_once(h, say)

    sess = h.sessions["100.1"]
    sid, iid = sess.ta_session_id, sess.ta_instance_id
    assert sid and iid
    author = h.orch.adapters[Provider.CLAUDE_CODE]
    assert sid not in author.archived

    sess.started_at -= st.ROUND_TTL + 1
    await ask_once(h, say)

    assert "하루가 지나" in say.messages[-1]["text"]
    assert sid in author.archived                                   # 워커를 반납했고
    assert all(r["instance_id"] != iid for r in h.orch.registry.active())   # 행도 지웠다
    assert "100.1" not in h.sessions


@pytest.mark.asyncio
async def test_expiry_notice_is_sent_once_per_round(tmp_path, repo, monkeypatch):
    """만료 안내에도 회차당 1회 게이트를 둔다. 없으면 그 스레드의 **모든** 답글마다
    안내가 나가고, tutor 메시지는 전부 채널 브로드캐스트라 그때마다 채널이 울린다 —
    owner 검사는 이 뒤에 있으므로 남의 답글에도 나간다 (최종 리뷰 I3).

    시계를 통째로 옮긴다: 메모리 세션의 `started_at`만 흔들면 trace 쪽은 아직
    싱싱해서, 세션이 반납된 다음 답글이 `_resume`으로 되살아나 버린다.
    """
    import time as _time

    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)
    later = _time.time() + 30 * 3600
    monkeypatch.setattr("devcrew.slack_tutor.time.time", lambda: later)

    n = len(say.messages)
    await ask_once(h, say)                                   # 첫 답글 — 안내
    assert len(say.messages) == n + 1 and "하루가 지나" in say.messages[-1]["text"]

    for who in ("U-OWNER", "U-STRANGER", "U-THIRD"):         # 이어지는 답글들
        await ask_once(h, say, user=who, text="그냥 잡담")
    assert len(say.messages) == n + 1                        # 더는 끼어들지 않는다


@pytest.mark.asyncio
async def test_expiry_notice_is_gated_on_the_trace_restore_path_too(tmp_path, repo,
                                                                    monkeypatch):
    """메모리 세션이 없으면 `_resume`이 매번 trace를 다시 읽고 매번 안내를 보냈다 —
    재시작 뒤가 오히려 더 시끄러웠던 자리다."""
    import time as _time

    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    h.sessions.clear()                                       # 프로세스 재시작
    later = _time.time() + 30 * 3600
    monkeypatch.setattr("devcrew.slack_tutor.time.time", lambda: later)

    n = len(say.messages)
    for _ in range(4):
        await ask_once(h, say)
    assert len(say.messages) == n + 1
    assert "하루가 지나" in say.messages[-1]["text"]


@pytest.mark.asyncio
async def test_idle_ta_session_is_reclaimed(tmp_path, repo):
    """스레드당 살아 있는 세션을 두는 설계에서 유휴 반납은 선택이 아니다 —
    하루 10회차면 일주일에 70개의 워커 서브프로세스가 쌓인다 (최종 리뷰 C3)."""
    import devcrew.slack_tutor as st

    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)
    await ask_once(h, say)
    sid = h.sessions["100.1"].ta_session_id

    h.sessions["100.1"].ta_touched -= st.TA_IDLE_TTL + 1
    await h._evict_ta()

    assert sid in h.orch.adapters[Provider.CLAUDE_CODE].archived
    assert "100.1" not in h.sessions


@pytest.mark.asyncio
async def test_evict_never_drops_a_round_that_is_still_being_answered(tmp_path, repo):
    """말없이 죽이면 학습자에겐 답이 끊긴 스레드만 남는다 (slack_brain과 같은 규칙)."""
    import devcrew.slack_tutor as st

    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)
    await ask_once(h, say)

    h.sessions["100.1"].ta_touched -= st.TA_IDLE_TTL + 1
    h.sessions["100.1"].ta_busy = True
    await h._evict_ta()
    assert "100.1" in h.sessions


@pytest.mark.asyncio
async def test_evict_drops_the_oldest_over_the_cap(tmp_path, repo):
    """상한이 없으면 유휴 TTL 안쪽에서도 무제한으로 쌓인다."""
    import devcrew.slack_tutor as st

    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)
    await ask_once(h, say)
    old = _idle_ta_session(h, "900.1", touched=h.sessions["100.1"].ta_touched - 60)

    orig, st.MAX_TA_SESSIONS = st.MAX_TA_SESSIONS, 1
    try:
        await h._evict_ta()
    finally:
        st.MAX_TA_SESSIONS = orig
    assert "900.1" not in h.sessions and "100.1" in h.sessions   # 오래된 쪽부터
    assert old in h.orch.adapters[Provider.CLAUDE_CODE].archived


def _idle_ta_session(h, thread_ts, *, touched, sid="fake-other"):
    """TA 세션이 열려 있는 다른 스레드의 회차를 하나 심는다 → 그 session_id.

    `sid`는 **세션마다 달라야** 어느 쪽이 반납됐는지 구분할 수 있다 — 기본값을 그대로
    둔 채 두 개를 심으면 두 세션이 같은 id를 갖고 단언이 통과할 수 없다.
    """
    from devcrew.slack_tutor import QuizSession
    sess = QuizSession(channel="C1", thread_ts=thread_ts, owner="U-OTHER",
                       repo_name="myrepo", questions=[], done=True)
    sess.ta_session_id = sid
    sess.ta_provider = Provider.CLAUDE_CODE
    sess.ta_instance_id = "tut-other"
    sess.ta_touched = touched
    h.sessions[thread_ts] = sess
    return sess.ta_session_id


@pytest.mark.asyncio
async def test_answering_a_question_reclaims_other_idle_ta_sessions(tmp_path, repo):
    """축출을 함수로만 두고 아무도 부르지 않으면 반납 경로가 없는 것과 같다 —
    배선 자체를 본다 (lessons C1)."""
    import devcrew.slack_tutor as st

    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)
    import time as _time
    old = _idle_ta_session(h, "900.1", touched=_time.monotonic() - st.TA_IDLE_TTL - 1)

    await ask_once(h, say)

    assert "900.1" not in h.sessions
    assert old in h.orch.adapters[Provider.CLAUDE_CODE].archived


# --- 최종 리뷰 fix (2026-08-24): owner 게이트(I6) · mrkdwn(I2) · repo(I4) · lock(I1) ---


@pytest.mark.asyncio
async def test_an_empty_owner_closes_the_gate_instead_of_opening_it(tmp_path, repo):
    """`if sess.owner and user != sess.owner`는 owner가 빈 문자열이면 통째로 False가
    되어 **누구나** 정답·근거·해설이 담긴 답변을 받는다. `_resume`이
    `payload.get("owner", "")`로 복원하므로 필드가 없던 옛 회차·손상된 페이로드가
    곧바로 개방이 된다 — 이 브랜치의 안전 속성 전체가 걸린 한 줄인데 실패 방향이
    열림이었다 (최종 리뷰 I6). fail-closed로 돌린다."""
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)
    h.sessions["100.1"].owner = ""            # 옛 회차 / 손상된 페이로드

    n = len(say.messages)
    await ask_once(h, say, user="U-ANYONE")

    assert len(say.messages) == n + 1
    assert "시작한 사람" in say.messages[-1]["text"]
    assert "후속 질문에 대한 답변" not in say.messages[-1]["text"]
    assert h.sessions["100.1"].ta_session_id is None     # 세션도 열지 않았다


@pytest.mark.asyncio
async def test_answer_text_is_converted_to_slack_mrkdwn(tmp_path, repo, monkeypatch):
    """문항은 전부 `rich()`를 거치는데 TA 답변만 원문 그대로 나가고 있었다.
    `**굵게**`는 날문자로 찍히고 `<T>`는 Slack이 엔티티로 먹어 통째로 사라진다 —
    코드를 설명하는 봇이라 둘 다 첫 사용에서 나온다 (최종 리뷰 I2, lessons C12).
    변환 뒤에도 잘림 표기와 인용 공개가 읽히는지 함께 고정한다."""
    import devcrew.slack_tutor as st
    from devcrew.tutor_ta import DROPPED_NOTE, TRUNCATED_NOTE, Answer

    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)

    body = ("**핵심은 여기다** — `Callable[<T>]`가 그 자리다"
            + TRUNCATED_NOTE + DROPPED_NOTE.format(n=2))

    async def fake_ask(*a, **kw):
        return Answer(session_id="s1", text=body, citations=[], dropped=2,
                      provider=Provider.CLAUDE_CODE, instance_id="i1")

    monkeypatch.setattr(st, "ask", fake_ask)
    await ask_once(h, say)

    out = say.messages[-1]["text"]
    assert "*핵심은 여기다*" in out and "**핵심은 여기다**" not in out
    assert "&lt;T&gt;" in out                       # Slack이 먹지 않도록 이스케이프됐다
    assert "_… 답변이 길어 잘렸습니다_" in out        # 잘림 표기가 살아남았고
    assert "_근거 2건은 대조에 실패해 제외했습니다_" in out   # 인용 공개도 살아남았다


@pytest.mark.asyncio
async def test_unregistered_repo_refuses_instead_of_reading_the_harness_repo(tmp_path, repo):
    """`repos.get(...) or ""`의 빈 문자열은 조용히 넘어가지 않는다: `worktree=""`는
    작업 디렉토리 고정을 건너뛰어 워커가 하네스 자기 repo에서 뜨고, `Path("")`는
    cwd라 인용 대조까지 하네스 기준이 된다 — **틀린 repo의 인용이 "대조 통과"로
    표시된다** (최종 리뷰 I4). 거짓을 가르치느니 답하지 않는다."""
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)
    h.repos.pop("myrepo")                  # repos.yaml에서 이름이 바뀐 뒤 재기동

    n = len(say.messages)
    await ask_once(h, say)

    assert len(say.messages) == n + 1
    assert "찾을 수 없어" in say.messages[-1]["text"]
    assert "후속 질문에 대한 답변" not in say.messages[-1]["text"]
    assert h.sessions["100.1"].ta_session_id is None     # 세션을 열지 않았다


def _graded_twin(h, thread_ts, owner):
    """이미 채점이 끝난 다른 스레드의 회차 — 같은 문항을 재사용한다."""
    from devcrew.slack_tutor import QuizSession
    src = h.sessions["100.1"]
    sess = QuizSession(channel="C1", thread_ts=thread_ts, owner=owner,
                       repo_name="myrepo", questions=list(src.questions),
                       answers=dict(src.answers), done=True)
    h.sessions[thread_ts] = sess
    return sess


@pytest.mark.asyncio
async def test_another_threads_turn_does_not_block_this_thread(tmp_path, repo):
    """lock이 `TutorHandler` 하나에 하나뿐이라 학습자 B가 **다른 스레드**에서 물어도
    남의 질문 때문에 최대 `TURN_TIMEOUT`(300초)을 기다렸고, 그러면서 존재하지도 않는
    "앞선 질문"에 대한 안내를 받았다. 스펙의 동시성 단위는 스레드다 (최종 리뷰 I1)."""
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)
    _graded_twin(h, "200.1", "U-B")

    await h._ta_lock_for("100.1").acquire()          # A의 질문이 처리 중
    try:
        n = len(say.messages)
        await asyncio.wait_for(
            h.on_question(thread_ts="200.1", text="왜?", user="U-B", say=say,
                          channel="C1"), timeout=10)
    finally:
        h._ta_lock_for("100.1").release()

    assert all("앞선 질문" not in m["text"] for m in say.messages[n:])
    assert "후속 질문에 대한 답변" in say.messages[-1]["text"]


@pytest.mark.asyncio
async def test_the_busy_notice_still_fires_within_the_same_thread(tmp_path, repo):
    """스레드별로 갈랐다고 같은 스레드의 안내까지 사라지면 안 된다 — 말해 주지 않으면
    무반응으로 보이고 학습자는 질문을 반복한다 (C14)."""
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)

    await h._ta_lock_for("100.1").acquire()
    try:
        task = asyncio.create_task(
            h.on_question(thread_ts="100.1", text="왜?", user="U-OWNER", say=say,
                          channel="C1"))
        for _ in range(50):
            await asyncio.sleep(0)
            if say.messages and "앞선 질문" in say.messages[-1]["text"]:
                break
        assert "앞선 질문" in say.messages[-1]["text"]
    finally:
        h._ta_lock_for("100.1").release()
    await asyncio.wait_for(task, timeout=10)
    assert "후속 질문에 대한 답변" in say.messages[-1]["text"]   # 풀리면 이어서 답한다


@pytest.mark.asyncio
async def test_has_round_survives_a_restart(tmp_path, repo):
    """라우팅(질문이냐 새 회차냐)을 메모리 세션만 보고 정하면, 재시작 뒤에는 채점이
    끝난 스레드에서 새 회차가 열린다 — 회차의 진실은 trace다."""
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    assert h.has_round("100.1") is True

    h.sessions.clear()                                # 프로세스 재시작
    assert h.has_round("100.1") is True
    assert h.has_round("999.9") is False


# --- Fix round 2 (2026-08-24): 축출 코드가 들여온 결함 두 건 ---


def _plain_round(h, thread_ts):
    """TA 세션이 없는 회차 — 진행 중이거나 아무도 질문하지 않은 스레드."""
    from devcrew.slack_tutor import QuizSession
    h.sessions[thread_ts] = QuizSession(channel="C1", thread_ts=thread_ts,
                                        owner="U-X", repo_name="myrepo", questions=[])


@pytest.mark.asyncio
async def test_the_cap_counts_live_ta_sessions_not_every_round(tmp_path, repo):
    """상한의 대상은 **살아 있는 워커**다. 분모를 `self.sessions`로 두면 TA 세션이
    없는 회차(진행 중·아무도 안 물어본 스레드)까지 세는데 그 dict는 `_drop` 말고는
    줄지 않는다 — 엔진이 회차 50개를 넘겨 본 뒤로는 조건이 영구히 참이 되어 매 답변마다
    `live`가 바닥까지 비워지고, 후보가 방금 답한 회차뿐이면 그 회차가 죽는다.
    누수는 아니지만 대화 연속성이 사라진다 — 이 기능의 존재 이유가 그것이다."""
    import devcrew.slack_tutor as st

    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)
    for i in range(60):                       # TA 세션이 없는 회차 60개
        _plain_round(h, f"8{i:02d}.1")
    assert len(h.sessions) > st.MAX_TA_SESSIONS

    await ask_once(h, say)
    sid = h.sessions["100.1"].ta_session_id if "100.1" in h.sessions else None
    assert sid, "방금 답한 회차가 자기 답변 뒤에 축출됐다"

    await ask_once(h, say, text="그럼 그건?")
    assert h.sessions["100.1"].ta_session_id == sid      # 같은 세션을 이어 쓴다
    # **이 회차의 TA 세션이** 반납되지 않았는지만 본다. 전역 `archived`가 비었는지
    # 보면 안 된다 — 출제/검증은 1회용이라 정상적으로 반납되고 여기에 쌓인다.
    assert sid not in h.orch.adapters[Provider.CLAUDE_CODE].archived


@pytest.mark.asyncio
async def test_an_answer_survives_being_evicted_mid_turn(tmp_path, repo):
    """`ta_busy`는 lock을 잡은 **뒤**에 세워진다. 게이트 통과부터 그 줄까지 사이에는
    `_set_status`(운영에선 실제 네트워크 호출)와 lock 대기가 있고, 그동안 이 회차는
    `live`에 남아 있다. 다른 스레드의 답변이 끝나며 `_evict_ta`를 돌리면 이 회차가
    `_drop`될 수 있고, 그러면 답변은 `self.sessions`에 없는 객체 위에서 끝나 새로 연
    세션의 손잡이가 아무 데도 남지 않는다 — C3가 없애려던 바로 그 상태다."""
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)

    sess = h.sessions["100.1"]

    async def evict_during_status(channel, thread_ts, text):
        # 게이트와 ta_busy 사이의 창 — 다른 스레드의 답변이 끝나며 축출이 돈다
        await h._drop(sess)

    h.status = evict_during_status
    await ask_once(h, say)

    assert "100.1" in h.sessions, "답변이 끝난 회차가 sessions에 없다 — 손잡이 소실"
    assert h.sessions["100.1"].ta_session_id
    assert h.sessions["100.1"].ta_session_id == sess.ta_session_id


@pytest.mark.asyncio
async def test_report_publish_failure_records_the_reason(tmp_path, repo):
    """리포트 발행이 실패하면 **사유를** trace에 남기고 스레드에도 한 줄 적는다.

    2026-08-24 16:29 실제로 발행이 실패했는데 `except Exception: url = None`이 사유를
    통째로 삼켜, 나중에 채점·렌더·업로드를 전부 다시 확인하고도 원인을 알 수 없었다.
    채점 자체는 이미 끝났으므로 회차를 되돌리지는 않는다 — 사유만 남긴다.
    """
    from devcrew.quiz import REPORT_FAILED_EVENT

    def boom(task_id, html):
        raise RuntimeError("R2 업로드 거부")

    h, trace, _ = make_handler(tmp_path, repo)
    h.publish = boom
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)

    reasons = [e["payload"]["reason"] for e in trace.events(event_type=REPORT_FAILED_EVENT)]
    assert any("R2 업로드 거부" in r for r in reasons), reasons
    assert any("R2 업로드 거부" in (m["text"] or "") for m in say.messages), \
        "사용자도 왜 링크가 없는지 알아야 한다"


@pytest.mark.asyncio
async def test_authoring_posts_a_line_per_stage(tmp_path, repo):
    """출제는 몇 분씩 걸린다 — 단계마다 한 줄을 적는다.

    2026-08-24 회차 시작부터 첫 문항까지 18분 동안 스레드가 완전히 조용했다.
    사용자에게는 무반응과 구분되지 않아 멘션을 반복하게 된다.
    """
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    joined = "\n".join(m["text"] or "" for m in say.messages)
    for stage in ("출제", "검증", "게시"):
        assert stage in joined, f"{stage} 단계 표시가 없다:\n{joined}"


@pytest.mark.asyncio
async def test_ta_answer_event_records_status_and_summary(tmp_path, repo):
    """TA 답변 이벤트에 `status`/`summary`를 남긴다.

    2026-08-24: 모델이 스키마 거절 루프에 걸려 `answer: "test"`, `summary: "test"`인
    최소 payload를 냈고 하네스는 그것을 정상 답변으로 서빙했다. 그때 trace에는 `answer`만
    있어서 "성실한 답인가 스키마를 때운 것인가"를 기록만으로 판별할 수 없었다 (C15).
    """
    from devcrew.quiz import TA_ANSWER_EVENT

    h, trace, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)
    await h.on_question(thread_ts="100.1", text="왜 그런가요?", say=say,
                        user="U-OWNER", channel="C1")

    evs = trace.events(event_type=TA_ANSWER_EVENT)
    assert evs, "TA 답변 이벤트가 없다"
    p = evs[-1]["payload"]
    assert "status" in p and "summary" in p, p


def _long_answer(n=2600):
    return ("*핵심*: 여기가 요점이다.\n\n" + "본문이 길게 이어진다. " * (n // 12))


@pytest.mark.asyncio
async def test_long_answer_goes_to_a_report_link(tmp_path, repo, monkeypatch):
    """Slack 한 메시지에 못 담는 답변은 HTML 리포트로 흘리고 머리말+링크만 남긴다.

    예전에는 2000자에서 잘라 "답변이 길어 잘렸습니다"만 남았고, 학습자는 나머지를 볼
    방법이 없었다. **trace에는 전문이 그대로 남는다** — 화면 표시가 기록을 깎으면 안 된다.
    """
    import devcrew.slack_tutor as st
    from devcrew.quiz import TA_ANSWER_EVENT
    from devcrew.tutor_ta import Answer

    body = _long_answer()
    h, trace, pub = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)

    async def fake_ask(*a, **kw):
        return Answer(session_id="s1", text=body, citations=[], dropped=0,
                      provider=Provider.CLAUDE_CODE, instance_id="i1")

    monkeypatch.setattr(st, "ask", fake_ask)
    await h.on_question(thread_ts="100.1", text="길게 설명해줘", say=say,
                        user="U-OWNER", channel="C1")

    last = say.messages[-1]["text"]
    assert "여기가 요점이다" in last, "무엇에 대한 답인지 스레드에서 보여야 한다"
    # URL은 본문이 아니라 **버튼**에 있다 (2026-08-25)
    assert _button_of(say.messages[-1])["url"].startswith("https://reports.example")
    assert len(last) < 1000, f"머리말만 남아야 한다 ({len(last)}자)"
    assert any("ta-" in tid for tid, _ in pub), [tid for tid, _ in pub]

    stored = trace.events(event_type=TA_ANSWER_EVENT)[-1]["payload"]["answer"]
    assert stored == body, "기록은 전문이어야 한다"


@pytest.mark.asyncio
async def test_short_answer_still_posts_inline(tmp_path, repo, monkeypatch):
    """짧은 답변까지 링크로 보내면 매번 브라우저를 열어야 한다 — 그대로 스레드에 쓴다."""
    import devcrew.slack_tutor as st
    from devcrew.tutor_ta import Answer

    h, _, pub = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)
    before = len(pub)

    async def fake_ask(*a, **kw):
        return Answer(session_id="s1", text="짧은 답이다.", citations=[], dropped=0,
                      provider=Provider.CLAUDE_CODE, instance_id="i1")

    monkeypatch.setattr(st, "ask", fake_ask)
    await h.on_question(thread_ts="100.1", text="왜?", say=say, user="U-OWNER",
                        channel="C1")

    assert say.messages[-1]["text"] == "짧은 답이다."
    assert len(pub) == before, "짧은 답변은 리포트를 만들지 않는다"


@pytest.mark.asyncio
async def test_report_failure_falls_back_to_the_truncated_body(tmp_path, repo, monkeypatch):
    """발행이 실패해도 답변은 준다 — 잘라서라도 보내고, 사유를 남긴다.

    링크를 못 만들었다고 답을 통째로 삼키면, 모델은 제대로 답했는데 학습자만 잃는다.
    """
    import devcrew.slack_tutor as st
    from devcrew.quiz import REPORT_FAILED_EVENT
    from devcrew.tutor_ta import Answer

    h, trace, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)

    def boom(task_id, html):
        raise RuntimeError("R2 거부")

    h.publish = boom

    async def fake_ask(*a, **kw):
        return Answer(session_id="s1", text=_long_answer(), citations=[], dropped=0,
                      provider=Provider.CLAUDE_CODE, instance_id="i1")

    monkeypatch.setattr(st, "ask", fake_ask)
    await h.on_question(thread_ts="100.1", text="길게", say=say, user="U-OWNER",
                        channel="C1")

    joined = "\n".join(m["text"] or "" for m in say.messages)
    assert "여기가 요점이다" in joined and "본문이 길게 이어진다" in joined
    reasons = [e["payload"]["reason"] for e in trace.events(event_type=REPORT_FAILED_EVENT)]
    assert any("R2 거부" in r for r in reasons), reasons


@pytest.mark.asyncio
async def test_code_question_produces_an_execution_report(tmp_path, repo, monkeypatch):
    """TA가 `code_focus`를 달면 실행 리포트를 만들어 링크를 준다.

    산문 열 줄보다 "코드 좌 / 상태 우, 2초에 한 칸"이 훨씬 잘 보인다는 것이 이 경로의
    이유다. 짧은 답변이라도 code_focus가 있으면 리포트를 만든다.
    """
    import devcrew.slack_tutor as st
    from devcrew.tutor_ta import Answer
    from devcrew.tutor_code import Line, Step, Trace, Var

    h, _, pub = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)

    async def fake_ask(*a, **kw):
        return Answer(session_id="s1", text="짧은 요약이다.", citations=[], dropped=0,
                      provider=Provider.CLAUDE_CODE, instance_id="i1",
                      code_focus={"path": "a.py", "symbol": "f"})

    async def fake_trace(*a, **kw):
        return Trace(title="a.py 실행", role_of_code="역할", path="a.py",
                     lines=[Line(number=1, text="line1"), Line(number=2, text="line2")],
                     steps=[Step(line=1, reason="첫 줄", vars=[Var("x", "1", True)]),
                            Step(line=2, reason="둘째 줄", vars=[])])

    monkeypatch.setattr(st, "ask", fake_ask)
    monkeypatch.setattr(st, "trace_code", fake_trace)
    await h.on_question(thread_ts="100.1", text="이 코드 어떻게 돌아?", say=say,
                        user="U-OWNER", channel="C1")

    last = say.messages[-1]["text"]
    assert "짧은 요약이다" in last
    assert _button_of(say.messages[-1])["url"].startswith("https://reports.example")
    assert any(tid.startswith("code-") for tid, _ in pub), [tid for tid, _ in pub]
    html = next(html for tid, html in pub if tid.startswith("code-"))
    assert "@keyframes" in html and "첫 줄" in html


@pytest.mark.asyncio
async def test_trace_failure_still_delivers_the_answer(tmp_path, repo, monkeypatch):
    """추적에 실패해도 답변은 준다 — 리포트는 곁다리지 답이 아니다."""
    import devcrew.slack_tutor as st
    from devcrew.tutor_ta import Answer
    from devcrew.tutor_code import TraceUnavailable

    h, trace, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)

    async def fake_ask(*a, **kw):
        return Answer(session_id="s1", text="그래도 이 답은 나가야 한다.", citations=[],
                      dropped=0, provider=Provider.CLAUDE_CODE, instance_id="i1",
                      code_focus={"path": "a.py", "symbol": None})

    async def boom(*a, **kw):
        raise TraceUnavailable("대상을 찾지 못했습니다")

    monkeypatch.setattr(st, "ask", fake_ask)
    monkeypatch.setattr(st, "trace_code", boom)
    await h.on_question(thread_ts="100.1", text="이 코드?", say=say, user="U-OWNER",
                        channel="C1")

    joined = "\n".join(m["text"] or "" for m in say.messages)
    assert "그래도 이 답은 나가야 한다" in joined


def _button_of(msg):
    for b in msg.get("blocks") or []:
        if b["type"] == "actions":
            return b["elements"][0]
    return None


@pytest.mark.asyncio
async def test_long_answer_report_link_is_a_button(tmp_path, repo, monkeypatch):
    """리포트 링크는 **버튼**으로 나간다 (사용자 2026-08-25).

    날 URL은 스레드에서 그냥 파란 글자라 다른 리포트(채점)와 모양이 어긋나고, 모바일에서
    눌러야 할 것인지도 덜 분명하다. 채점 리포트가 이미 `tutor_report` 버튼을 쓴다.
    `text`는 알림 미리보기로 읽히므로 URL이 아니라 사람이 읽을 문장을 남긴다.
    """
    import devcrew.slack_tutor as st
    from devcrew.tutor_ta import Answer

    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)

    async def fake_ask(*a, **kw):
        return Answer(session_id="s1", text=_long_answer(), citations=[], dropped=0,
                      provider=Provider.CLAUDE_CODE, instance_id="i1")

    monkeypatch.setattr(st, "ask", fake_ask)
    await h.on_question(thread_ts="100.1", text="길게", say=say, user="U-OWNER",
                        channel="C1")

    btn = _button_of(say.messages[-1])
    assert btn and btn["type"] == "button", say.messages[-1]
    assert btn["url"].startswith("https://reports.example")
    assert btn["action_id"] == "tutor_report"
    # **화면에 보이는 것**(section)에는 날 URL이 없다. `text`는 blocks가 있으면
    # Slack이 표시하지 않고 알림 미리보기·폴백으로만 쓰므로 링크를 남겨 둔다 —
    # blocks를 못 받는 say로 되돌아갈 때 링크가 사라지지 않게.
    section = say.messages[-1]["blocks"][0]["text"]["text"]
    assert "여기가 요점이다" in section and "https://" not in section
    assert "https://reports.example" in say.messages[-1]["text"], "폴백에 링크가 없다"


@pytest.mark.asyncio
async def test_code_report_link_is_a_button(tmp_path, repo, monkeypatch):
    """실행 리포트도 같다 — 버튼 문구만 다르다."""
    import devcrew.slack_tutor as st
    from devcrew.tutor_ta import Answer
    from devcrew.tutor_code import Line, Step, Trace

    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)

    async def fake_ask(*a, **kw):
        return Answer(session_id="s1", text="짧은 요약이다.", citations=[], dropped=0,
                      provider=Provider.CLAUDE_CODE, instance_id="i1",
                      code_focus={"path": "a.py", "symbol": "f"})

    async def fake_trace(*a, **kw):
        return Trace(title="t", role_of_code="r", path="a.py",
                     lines=[Line(1, "x"), Line(2, "y")],
                     steps=[Step(1, "첫 줄", []), Step(2, "둘째 줄", [])])

    monkeypatch.setattr(st, "ask", fake_ask)
    monkeypatch.setattr(st, "trace_code", fake_trace)
    await h.on_question(thread_ts="100.1", text="이 코드 어떻게 돌아?", say=say,
                        user="U-OWNER", channel="C1")

    btn = _button_of(say.messages[-1])
    assert btn and btn["type"] == "button", say.messages[-1]
    assert "실행" in btn["text"]["text"]
    assert "https://" not in say.messages[-1]["blocks"][0]["text"]["text"]


@pytest.mark.asyncio
async def test_blocks_are_valid_and_within_slack_limits(tmp_path, repo, monkeypatch):
    """버튼 라벨은 `plain_text`라 마크다운이 없고 길이 상한이 있다."""
    import devcrew.slack_tutor as st
    from devcrew.tutor_ta import Answer

    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)

    async def fake_ask(*a, **kw):
        return Answer(session_id="s1", text=_long_answer(4000), citations=[], dropped=3,
                      provider=Provider.CLAUDE_CODE, instance_id="i1")

    monkeypatch.setattr(st, "ask", fake_ask)
    await h.on_question(thread_ts="100.1", text="길게", say=say, user="U-OWNER",
                        channel="C1")
    assert_block_kit_valid(say.messages[-1]["blocks"])


@pytest.mark.asyncio
async def test_diagram_spec_reaches_the_report(tmp_path, repo, monkeypatch):
    """tutor가 `diagram`을 달면 그림이 리포트에 실린다 (호출자→피호출자 배선).

    순수 함수 테스트는 배선을 증명하지 않는다 — 이 저장소는 같은 이유로 배선을 세 번
    조용히 잃었다(lessons C1). spec이 `draw()`까지 가고 그 결과가 HTML에 들어가는지 본다.
    """
    import devcrew.slack_tutor as st
    from devcrew.tutor_ta import Answer

    seen = {}
    h, _, pub = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)

    async def fake_ask(*a, **kw):
        return Answer(session_id="s1", text=_long_answer(), citations=[], dropped=0,
                      provider=Provider.CLAUDE_CODE, instance_id="i1",
                      diagram="정지 → 확인 → 기동 흐름도")

    async def fake_draw(orch, cfg, *, exec_id, spec):
        seen["spec"] = spec
        return '<svg viewBox="0 0 10 10"><title>흐름</title></svg>'

    monkeypatch.setattr(st, "ask", fake_ask)
    monkeypatch.setattr(st, "draw", fake_draw)
    await h.on_question(thread_ts="100.1", text="설명해줘", say=say, user="U-OWNER",
                        channel="C1")

    assert seen.get("spec") == "정지 → 확인 → 기동 흐름도", "spec이 draw까지 안 갔다"
    html = next(h for tid, h in pub if tid.startswith("ta-"))
    assert "<svg viewBox=" in html and "흐름" in html


@pytest.mark.asyncio
async def test_report_is_complete_without_a_diagram(tmp_path, repo, monkeypatch):
    """그림은 곁다리다 — 못 그렸다고 답변을 막으면 안 된다."""
    import devcrew.slack_tutor as st
    from devcrew.tutor_ta import Answer

    h, _, pub = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)

    async def fake_ask(*a, **kw):
        return Answer(session_id="s1", text=_long_answer(), citations=[], dropped=0,
                      provider=Provider.CLAUDE_CODE, instance_id="i1",
                      diagram="그릴 수 없는 것")

    async def fake_draw(*a, **kw):
        return None

    monkeypatch.setattr(st, "ask", fake_ask)
    monkeypatch.setattr(st, "draw", fake_draw)
    await h.on_question(thread_ts="100.1", text="설명해줘", say=say, user="U-OWNER",
                        channel="C1")

    html = next(h for tid, h in pub if tid.startswith("ta-"))
    assert "<svg" not in html and "여기가 요점이다" in html
    assert _button_of(say.messages[-1]), "링크는 그대로 나가야 한다"


@pytest.mark.asyncio
async def test_code_report_carries_its_diagram(tmp_path, repo, monkeypatch):
    """실행 리포트도 같다 — TUTOR_CODE가 낸 spec으로 그린다."""
    import devcrew.slack_tutor as st
    from devcrew.tutor_ta import Answer
    from devcrew.tutor_code import Line, Step, Trace

    h, _, pub = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(), say)
    await answer_all(h, say)

    async def fake_ask(*a, **kw):
        return Answer(session_id="s1", text="요약", citations=[], dropped=0,
                      provider=Provider.CLAUDE_CODE, instance_id="i1",
                      code_focus={"path": "a.py", "symbol": None})

    async def fake_trace(*a, **kw):
        return Trace(title="t", role_of_code="r", path="a.py",
                     lines=[Line(1, "x"), Line(2, "y")],
                     steps=[Step(1, "a", []), Step(2, "b", [])],
                     diagram="루프 구조")

    async def fake_draw(orch, cfg, *, exec_id, spec):
        assert spec == "루프 구조"
        return '<svg viewBox="0 0 10 10"><title>루프</title></svg>'

    monkeypatch.setattr(st, "ask", fake_ask)
    monkeypatch.setattr(st, "trace_code", fake_trace)
    monkeypatch.setattr(st, "draw", fake_draw)
    await h.on_question(thread_ts="100.1", text="이 코드?", say=say, user="U-OWNER",
                        channel="C1")

    html = next(h for tid, h in pub if tid.startswith("code-"))
    assert "<svg viewBox=" in html and "루프" in html


@pytest.mark.asyncio
async def test_sweep_idle_reclaims_sessions_past_the_ttl(tmp_path, repo):
    """TTL이 지난 TA 세션은 아무도 다시 묻지 않아도 반납된다 (사용자 2026-08-25).

    축출은 지금까지 **답변 직후에만** 돌았다 — 그래서 마지막 질문 뒤 아무도 안 물으면
    TTL이 지나도 세션이 남았다. 실제로 17시간 산 워커를 정리 중에 발견했다.
    """
    import devcrew.slack_tutor as st

    h, _, _ = make_handler(tmp_path, repo)
    stale = _idle_ta_session(h, "900.1", touched=time.monotonic() - st.TA_IDLE_TTL - 60,
                             sid="stale-1")
    fresh = _idle_ta_session(h, "900.2", touched=time.monotonic(), sid="fresh-2")

    await h.sweep_idle()

    archived = h.orch.adapters[Provider.CLAUDE_CODE].archived
    assert stale in archived, "TTL이 지난 세션을 반납하지 않았다"
    assert fresh not in archived, "아직 살아 있는 세션까지 죽였다"
    assert "900.1" not in h.sessions and "900.2" in h.sessions


@pytest.mark.asyncio
async def test_sweep_never_touches_a_session_mid_answer(tmp_path, repo):
    """답변 중인 회차를 말없이 죽이면 학습자에겐 답이 끊긴 스레드만 남는다."""
    import devcrew.slack_tutor as st

    h, _, _ = make_handler(tmp_path, repo)
    sid = _idle_ta_session(h, "900.3", touched=time.monotonic() - st.TA_IDLE_TTL - 60)
    h.sessions["900.3"].ta_busy = True

    await h.sweep_idle()
    assert sid not in h.orch.adapters[Provider.CLAUDE_CODE].archived
    assert "900.3" in h.sessions


@pytest.mark.asyncio
async def test_sweep_loop_actually_calls_the_sweep(tmp_path, repo):
    """**배선 테스트.** 루프가 실제로 sweep을 부르는지 본다 — 순수 함수 테스트는
    배선을 증명하지 않는다 (lessons C1). 이 저장소는 같은 이유로 배선을 세 번 잃었다."""
    import asyncio

    from devcrew.slack_tutor import sweep_loop

    h, _, _ = make_handler(tmp_path, repo)
    calls = []
    h.sweep_idle = lambda: calls.append(1) or asyncio.sleep(0)

    task = asyncio.create_task(sweep_loop(h, interval=0.01))
    await asyncio.sleep(0.06)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert len(calls) >= 2, f"루프가 sweep을 부르지 않았다 ({len(calls)}회)"


@pytest.mark.asyncio
async def test_sweep_loop_survives_a_failing_sweep(tmp_path, repo):
    """쓸어담기 하나가 실패해도 루프가 죽으면 안 된다 — 그러면 이후 전부 안 돈다."""
    import asyncio

    from devcrew.slack_tutor import sweep_loop

    h, _, _ = make_handler(tmp_path, repo)
    calls = []

    async def boom():
        calls.append(1)
        raise RuntimeError("registry 접근 실패")

    h.sweep_idle = boom
    task = asyncio.create_task(sweep_loop(h, interval=0.01))
    await asyncio.sleep(0.06)
    alive = not task.done()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert alive and len(calls) >= 2, f"루프가 죽었다 ({len(calls)}회 호출)"


# ── 주제 모드 (§10.8) ────────────────────────────────────────────────────────
def make_topic_handler(tmp_path, repo, *, n=12, report="핵심 요약 문단.\n\n본문 이어짐."):
    """조사 → 출제 순서로 응답을 깔아 둔 핸들러. 자료 파일은 워커 대신 테스트가 쓴다
    (FakeAdapter는 파일을 만들지 않는다)."""
    trace = TraceStore(tmp_path / "t.db")
    author = Scripted([
        {"status": "PASS", "summary": "조사", "report": report,
         "sources": [{"url": "https://example.org/spec", "title": "스펙"}],
         "citations": [{"path": "01.md", "start_line": 1, "end_line": 1,
                        "quote": "자료 문장 1"}], "diagram": None},
        {"status": "PASS", "summary": "s",
         "questions": [_q(i, start=i + 1) for i in range(n)]},
        {"answer": "후속 답변.", "citations": []},
    ])
    verifier = Scripted([{"status": "PASS", "summary": "v",
                          "verdicts": [{"index": i, "verdict": "PASS", "reason": "ok"}
                                       for i in range(n)]}])
    orch = Orchestrator(trace, SessionRegistry(tmp_path / "h.db"),
                        {Provider.CLAUDE_CODE: author, Provider.CODEX: verifier})
    from devcrew.slack_tutor import TutorHandler
    pub = []
    h = TutorHandler(orch, load_config(), {"myrepo": repo},
                     publish=lambda tid, html: (pub.append((tid, html)),
                                                f"https://reports.example/{tid}")[1],
                     corpus_root=tmp_path / "corpus")
    return h, trace, pub


def seed_corpus(h, thread_ts="100.1", *, lines=40, source=True):
    """워커가 저장했을 자료. **출처 머리글까지** 넣는다 — 하네스가 파일을 열어 확인하고,
    없으면 리포트 자체를 거절한다 (신뢰도 관문).

    `01.md`는 조사 인용 대상, `a.py`는 출제 evidence 대상이다. 관문은 확장자를 가리지
    않으므로 둘 다 출처가 있어야 한다.
    """
    d = h.corpus_dir_for(thread_ts)
    d.mkdir(parents=True, exist_ok=True)
    # 출처 줄을 **끝에** 붙인다 — 앞에 두면 줄 번호가 밀려 evidence(`파일:N` = `lineN`)와
    # 어긋난다. 검사는 파일 어디에 있든 찾는다.
    tail = "\n> 출처: https://example.org/doc\n" if source else "\n"
    (d / "01.md").write_text(
        "\n".join(f"자료 문장 {i}" for i in range(1, 10)) + tail)
    (d / "a.py").write_text(
        "\n".join(f"line{i}" for i in range(1, lines)) + tail)
    return d


@pytest.mark.asyncio
async def test_topic_mention_researches_and_offers_the_quiz_button(tmp_path, repo):
    """접두 없는 문장 = 주제 학습. **출제는 하지 않고** 리포트와 버튼을 낸다 (D2)."""
    h, _, pub = make_topic_handler(tmp_path, repo)
    seed_corpus(h)
    say = SaySpy()

    await h.on_mention(mention(text="<@U1> Kafka 리밸런싱"), say)

    last = say.messages[-1]
    assert "핵심 요약 문단" in last["text"]           # 첫 문단만 스레드에 남는다
    assert "본문 이어짐" not in last["text"]          # 나머지는 리포트로
    assert "https://reports.example/" in last["text"]
    assert [b["value"] for b in buttons_of(last)] == ["__START_QUIZ__"]
    assert h.sessions == {}                            # 아직 회차가 아니다
    assert pub and "출처" in pub[-1][1]                # 리포트에 출처 절이 있다


@pytest.mark.asyncio
async def test_quiz_button_issues_from_the_corpus_not_a_repo(tmp_path, repo, monkeypatch):
    """**배선 테스트** — 버튼이 누른 회차의 근거가 corpus 디렉토리여야 한다.

    여기서 registry 경로가 새면 학습자는 방금 조사한 주제 대신 엉뚱한 repo로 시험을
    본다. 값이 러너→핸들러→파이프라인으로 흐르는지 실제 호출 인자로 고정한다.
    """
    import devcrew.slack_tutor as st

    h, _, _ = make_topic_handler(tmp_path, repo)
    corpus = seed_corpus(h)
    say = SaySpy()
    await h.on_mention(mention(text="<@U1> Kafka 리밸런싱"), say)

    seen = {}
    real = st.issue_quiz

    async def spy(*a, **kw):
        seen.update(kw)
        return await real(*a, **kw)

    monkeypatch.setattr(st, "issue_quiz", spy)
    await h.on_answer(thread_ts="100.1", value="__START_QUIZ__", say=say,
                      user="U-OWNER", channel="C1")

    assert seen["repo_path"] == str(corpus)            # registry가 아니라 corpus
    assert "1 / 10" in say.messages[-1]["text"]        # 회차가 실제로 열렸다
    assert h.sessions["100.1"].corpus_dir == str(corpus)


@pytest.mark.asyncio
async def test_quiz_button_is_owner_only(tmp_path, repo):
    h, _, _ = make_topic_handler(tmp_path, repo)
    seed_corpus(h)
    say = SaySpy()
    await h.on_mention(mention(text="<@U1> 주제"), say)
    await h.on_answer(thread_ts="100.1", value="__START_QUIZ__", say=say,
                      user="U-STRANGER", channel="C1")
    assert "시작한 사람만" in say.messages[-1]["text"]
    assert h.sessions == {}


@pytest.mark.asyncio
async def test_quiz_button_refuses_when_the_corpus_is_gone(tmp_path, repo):
    """자료가 회수된 뒤 빈 경로로 출제하면 워커가 하네스 repo에서 뜨고 엉뚱한 인용이
    '대조 통과'로 표시된다 — 그럴 바에는 열지 않는다."""
    import shutil

    h, _, _ = make_topic_handler(tmp_path, repo)
    corpus = seed_corpus(h)
    say = SaySpy()
    await h.on_mention(mention(text="<@U1> 주제"), say)
    shutil.rmtree(corpus)

    await h.on_answer(thread_ts="100.1", value="__START_QUIZ__", say=say,
                      user="U-OWNER", channel="C1")
    assert "만료" in say.messages[-1]["text"]
    assert h.sessions == {}


@pytest.mark.asyncio
async def test_quiz_button_survives_a_restart_via_trace(tmp_path, repo):
    """리포트를 읽는 동안 브리지가 재시작되면 메모리 상태가 없다 — 버튼이 死문자가
    되면 사용자에게는 '눌러도 아무 일이 없다'로만 보인다."""
    h, _, _ = make_topic_handler(tmp_path, repo)
    corpus = seed_corpus(h)
    say = SaySpy()
    await h.on_mention(mention(text="<@U1> 주제"), say)

    h.topics.clear()                                   # 프로세스 재시작 흉내
    await h.on_answer(thread_ts="100.1", value="__START_QUIZ__", say=say,
                      user="U-OWNER", channel="C1")
    assert h.sessions["100.1"].corpus_dir == str(corpus)


@pytest.mark.asyncio
async def test_followup_answers_from_the_corpus(tmp_path, repo, monkeypatch):
    """채점 뒤 후속 질문의 근거도 corpus다 — registry로 풀면 대상이 통째로 바뀐다."""
    import devcrew.slack_tutor as st

    h, _, _ = make_topic_handler(tmp_path, repo)
    corpus = seed_corpus(h)
    say = SaySpy()
    await h.on_mention(mention(text="<@U1> 주제"), say)
    await h.on_answer(thread_ts="100.1", value="__START_QUIZ__", say=say,
                      user="U-OWNER", channel="C1")
    await answer_all(h, say, correct=10)

    seen = {}
    real = st.ask

    async def spy(*a, **kw):
        seen.update(kw)
        return await real(*a, **kw)

    monkeypatch.setattr(st, "ask", spy)
    await h.on_question(thread_ts="100.1", text="왜 그런가?", say=say,
                        user="U-OWNER", channel="C1")
    assert seen["repo_path"] == str(corpus)


def test_sweep_corpus_removes_expired_but_keeps_busy_rounds(tmp_path, repo):
    """일회성이 전제다 — 안 지우면 최신화되지 않는 스냅샷만 쌓인다. 다만 답변 중인
    회차의 자료는 TA가 지금 읽고 있으므로 건드리지 않는다."""
    import os

    from devcrew.slack_tutor import QuizSession

    h, _, _ = make_topic_handler(tmp_path, repo)
    fresh = seed_corpus(h, "200.1")
    stale = seed_corpus(h, "300.1")
    busy = seed_corpus(h, "400.1")
    old = time.time() - 30 * 3600
    for d in (stale, busy):
        os.utime(d, (old, old))
    h.sessions["400.1"] = QuizSession(channel="C1", thread_ts="400.1", owner="U",
                                      repo_name="주제", questions=[],
                                      corpus_dir=str(busy), ta_busy=True)

    removed = h.sweep_corpus()

    assert removed == ["QUIZ-300.1"]
    assert fresh.is_dir() and busy.is_dir() and not stale.exists()


@pytest.mark.asyncio
async def test_material_without_a_source_never_reaches_the_learner(tmp_path, repo):
    """출처 없는 자료로는 리포트도 회차도 열리지 않는다 — 사유는 그대로 스레드에.

    화면에 "성실한 리포트"로 보이는 순간 그 자료는 출제 근거가 되고, 거짓이 문항으로
    굳는다. 막는 자리는 학습자에게 보이기 **전**이어야 한다.
    """
    h, _, pub = make_topic_handler(tmp_path, repo)
    seed_corpus(h, source=False)
    say = SaySpy()

    await h.on_mention(mention(text="<@U1> 주제"), say)

    text = say.messages[-1]["text"]
    assert "출처 표시가 없는" in text          # 무엇이 없어서 거절했는지 말한다
    assert pub == []                            # 리포트를 발행하지 않았다
    assert h.topics == {} and h.sessions == {}  # 버튼도 회차도 남기지 않는다


class UpdateSpy:
    """chat_update 대역 — (ts, text, blocks) 기록."""

    def __init__(self):
        self.calls = []

    async def __call__(self, channel, ts, text, blocks=None):
        self.calls.append({"ts": ts, "text": text, "blocks": blocks})


@pytest.mark.asyncio
async def test_progress_edits_one_message_instead_of_stacking(tmp_path, repo):
    """진행 표시는 **한 메시지를 갱신**한다. 단계마다 새 메시지를 쌓으면 스레드가
    회차 내용보다 진행 로그로 길어지고, 끝난 뒤에도 남는다."""
    h, _, _ = make_topic_handler(tmp_path, repo)
    seed_corpus(h)
    h.update = UpdateSpy()
    say = SaySpy()

    await h.on_mention(mention(text="<@U1> 주제"), say)

    # 스레드에 새로 올라간 것은 첫 진행 메시지 하나뿐이다
    assert len(say.messages) == 1
    assert say.messages[0]["text"].startswith("⏳")
    updates = h.update.calls
    assert all(u["ts"] == "msg-1" for u in updates)          # 같은 메시지를 계속 고친다
    # 마지막 갱신이 곧 결과 카드다 — "⏳ 리포트 만드는 중…"이 남지 않는다
    assert "주제" in updates[-1]["text"] and "📄" in updates[-1]["text"]
    assert [b["value"] for b in buttons_of(updates[-1])] == ["__START_QUIZ__"]


@pytest.mark.asyncio
async def test_progress_falls_back_to_new_messages_without_update(tmp_path, repo):
    """`update`가 없으면 새 메시지를 올리는 종전 동작으로 떨어진다 —
    진행이 안 보이는 것이 중복보다 나쁘다 (침묵은 고장과 구분되지 않는다)."""
    h, _, _ = make_topic_handler(tmp_path, repo)
    seed_corpus(h)
    say = SaySpy()

    await h.on_mention(mention(text="<@U1> 주제"), say)

    assert len(say.messages) > 1
    assert [b["value"] for b in buttons_of(say.messages[-1])] == ["__START_QUIZ__"]


@pytest.mark.asyncio
async def test_upload_failure_keeps_the_research_and_the_button(tmp_path, repo):
    """발행 실패가 조사를 통째로 날리면 안 된다 (2026-09-04: wrangler 60초 상한).

    자료는 이미 디스크에 있고 본문도 손에 있다 — 없는 것은 링크뿐이다. 링크 하나
    때문에 10분짜리 조사와 출제 버튼까지 잃었다.
    """
    from devcrew.quiz import REPORT_FAILED_EVENT

    h, trace, _ = make_topic_handler(tmp_path, repo)
    seed_corpus(h)

    def boom(task_id, html):
        raise TimeoutError("wrangler timed out after 60 seconds")

    h.publish = boom
    say = SaySpy()

    await h.on_mention(mention(text="<@U1> 주제"), say)

    last = say.messages[-1]
    assert "핵심 요약 문단" in last["text"]                    # 본문은 살아 있다
    assert "📄" not in last["text"]                            # 링크만 없다
    assert [b["value"] for b in buttons_of(last)] == ["__START_QUIZ__"]   # 출제는 가능
    assert h.topics["100.1"]["corpus_dir"]                     # 대상도 남았다
    evs = trace.events(event_type=REPORT_FAILED_EVENT)
    assert evs and evs[-1]["payload"]["kind"] == "research"    # 사유는 trace에


@pytest.mark.asyncio
async def test_heartbeat_shows_elapsed_and_material_count(tmp_path, repo, monkeypatch):
    """조사 한 단계가 실측 10분이다 — 그동안 글자가 안 바뀌면 멈춘 것과 구분되지 않는다.

    경과 시간과 저장된 자료 수는 **하네스가 시계와 디렉토리에서 직접 읽는다.**
    워커에게 묻지 않으므로 모델 주장과 실제가 갈릴 여지도 없다.
    """
    import devcrew.slack_tutor as st

    monkeypatch.setattr(st, "HEARTBEAT_INTERVAL", 0.01)
    h, _, _ = make_topic_handler(tmp_path, repo)
    seed_corpus(h)
    h.update = UpdateSpy()
    say = SaySpy()

    real = st.research          # **패치 전에** 붙잡는다 — 안 그러면 자기를 다시 부른다

    async def slow_research(*a, **kw):
        await kw["progress"]("자료 조사 중…")
        await asyncio.sleep(0.06)          # 하트비트가 여러 번 돌 시간
        return await real(*a, **kw)

    monkeypatch.setattr(st, "research", slow_research)
    await h.on_mention(mention(text="<@U1> 주제"), say)

    beats = [u["text"] for u in h.update.calls if u["text"].startswith("⏳")]
    assert beats, "하트비트가 한 번도 안 돌았다"
    assert any("자료 조사 중…" in b and "자료 2개" in b for b in beats)
    assert any("초" in b for b in beats)
    # 마지막 갱신은 결과 카드다 — 하트비트가 그걸 되돌리지 않는다
    assert h.update.calls[-1]["text"].startswith("🎓")
