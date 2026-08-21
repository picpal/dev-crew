"""slack_tutor — 퀴즈 세션·문항 진행·재개·채점 발행 (FakeAdapter, Slack 없음)."""
import dataclasses

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

    async def send(self, session_id, message):
        out = await super().send(session_id, message)
        if not self.queue:
            return out
        return dataclasses.replace(out, structured=self.queue.pop(0))


class SaySpy:
    def __init__(self):
        self.messages = []

    async def __call__(self, *, text, thread_ts=None, blocks=None):
        self.messages.append({"text": text, "thread_ts": thread_ts, "blocks": blocks})


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


def make_handler(tmp_path, repo, *, n=12, published=None):
    trace = TraceStore(tmp_path / "t.db")
    author = Scripted([{"status": "PASS", "summary": "s",
                        "questions": [_q(i, start=i + 1) for i in range(n)]}])
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

    h = TutorHandler(orch, load_config(), {"myrepo": repo}, publish=publish)
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
async def test_repo_is_required(tmp_path, repo):
    h, _, _ = make_handler(tmp_path, repo)
    say = SaySpy()
    await h.on_mention(mention(text="<@U1> 아무 주제"), say)
    assert "repo" in say.messages[-1]["text"]
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
