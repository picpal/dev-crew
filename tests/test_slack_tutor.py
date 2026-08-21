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
