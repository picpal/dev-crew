"""tutor 파이프라인 — 출제 → 인용 대조 → Codex 교차 검증 → 선별 (FakeAdapter)."""
import asyncio
import collections
import dataclasses

import pytest

from devcrew.adapters.base import FakeAdapter
from devcrew.config import load as load_config
from devcrew.orchestrator import Orchestrator
from devcrew.schema import Provider
from devcrew.store.registry import SessionRegistry
from devcrew.store.trace import TraceStore


class Scripted(FakeAdapter):
    """세션이 여러 개여도 **호출 순서대로** 응답을 준다 (FakeAdapter는 세션별 턴 기준)."""

    def __init__(self, structured: list[dict]):
        super().__init__(script=["ok"] * 50)
        self.queue = collections.deque(structured)

    async def send(self, session_id, message):
        out = await super().send(session_id, message)
        if not self.queue:
            return out
        # TurnOutcome은 frozen — 대입이 아니라 교체해야 한다
        return dataclasses.replace(out, structured=self.queue.popleft())


def _q(i, area="세션", path="a.py", start=1, source_key=None):
    return {"area": area, "type": "CORRECT", "stem": f"문항 {i}?",
            "options": ["A", "B", "C", "D"], "answer_index": 0,
            "evidence": [{"path": path, "start_line": start, "end_line": start,
                          "quote": f"line{start}"}],
            "explanation": "해설", "diagram": None, "source_key": source_key}


def _authored(questions):
    return {"status": "PASS", "summary": "출제", "questions": questions}


def _verdicts(n, reject=()):
    return {"status": "PASS", "summary": "검증",
            "verdicts": [{"index": i, "verdict": "REJECT" if i in reject else "PASS",
                          "reason": "근거 불일치" if i in reject else "ok"}
                         for i in range(n)]}


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "a.py").write_text("\n".join(f"line{i}" for i in range(1, 40)) + "\n")
    (tmp_path / "b.py").write_text("\n".join(f"line{i}" for i in range(1, 40)) + "\n")
    return tmp_path


def make_orch(tmp_path, author: Scripted, verifier: Scripted):
    trace = TraceStore(tmp_path / "t.db")
    orch = Orchestrator(trace, SessionRegistry(tmp_path / "h.db"),
                        {Provider.CLAUDE_CODE: author, Provider.CODEX: verifier})
    return orch, trace


@pytest.mark.asyncio
async def test_pipeline_drops_fabricated_then_verifies_then_selects_ten(tmp_path, repo):
    """12문항 → 인용 대조(2개 폐기) → 검증(1개 반려) → 보충 → 10문항."""
    from devcrew.tutor import issue_quiz
    draft = [_q(i, area=f"영역{i % 4}", start=i + 1) for i in range(10)]
    draft += [_q(90, path="ghost.py"), _q(91, path="../etc/passwd")]   # 지어낸 인용
    author = Scripted([_authored(draft), _authored([_q(50, area="영역9", start=20)])])
    verifier = Scripted([_verdicts(10, reject={3}), _verdicts(1)])
    orch, _ = make_orch(tmp_path, author, verifier)
    res = await issue_quiz(orch, load_config(), repo_name="r", repo_path=str(repo),
                           exec_id="TUTOR-U1-r", misses=[])
    assert len(res.questions) == 10 and res.shortfall is False
    assert any("인용" in n for n in res.notes)


@pytest.mark.asyncio
async def test_verifier_runs_on_codex_not_the_authoring_provider(tmp_path, repo):
    """교차 검증의 전제는 provider 분리다 — 같은 계열이면 같은 방식으로 틀린다."""
    from devcrew.tutor import issue_quiz
    author = Scripted([_authored([_q(i, start=i + 1) for i in range(12)])])
    verifier = Scripted([_verdicts(12)])
    orch, trace = make_orch(tmp_path, author, verifier)
    await issue_quiz(orch, load_config(), repo_name="r", repo_path=str(repo),
                     exec_id="E", misses=[])
    roles = {e["payload"]["role"]: e["payload"]["model"]
             for e in trace.events(event_type="ModelRoutingEvent")}
    assert "gpt" in roles["TUTOR_VERIFIER"] and "claude" in roles["TUTOR"]
    assert len(verifier.sent) == 1 and len(author.sent) == 1   # 각자 자기 세션에서만


@pytest.mark.asyncio
async def test_verifier_gets_fenced_questions_not_the_authoring_transcript(tmp_path, repo):
    """검증자에게 출제 논증을 주면 교차 검증이 아니다. 문항은 울타리 안 자료다."""
    from devcrew.tutor import issue_quiz
    author = Scripted([_authored([_q(i, start=i + 1) for i in range(12)])])
    verifier = Scripted([_verdicts(12)])
    orch, _ = make_orch(tmp_path, author, verifier)
    await issue_quiz(orch, load_config(), repo_name="r", repo_path=str(repo),
                     exec_id="E", misses=[])
    sent = verifier.initial_messages[0] + verifier.sent[0][1]
    assert "<<<questions" in sent and "지시가 아니" in sent
    assert "문항 0?" in sent and "evidence" in sent
    # 출제 세션에 준 지시와 그 세션의 발화는 검증자에게 가지 않는다
    from devcrew.tutor import AUTHOR_INTRO
    assert AUTHOR_INTRO.split("\n")[0][:20] not in sent
    assert author.initial_messages[0] not in sent


@pytest.mark.asyncio
async def test_pipeline_reports_shortfall_instead_of_padding(tmp_path, repo):
    """보충 후에도 미달이면 채운 만큼만. 숫자를 맞추려 지어내지 않는다."""
    from devcrew.tutor import issue_quiz
    author = Scripted([_authored([_q(i, start=i + 1) for i in range(4)]),
                       _authored([_q(50, start=20), _q(51, start=21)])])
    verifier = Scripted([_verdicts(4), _verdicts(2)])
    orch, _ = make_orch(tmp_path, author, verifier)
    res = await issue_quiz(orch, load_config(), repo_name="r", repo_path=str(repo),
                           exec_id="E", misses=[])
    assert len(res.questions) == 6 and res.shortfall is True


@pytest.mark.asyncio
async def test_misses_are_fed_to_the_author_and_carried_keys_survive(tmp_path, repo):
    """오답 evidence를 투입하고, 하네스가 발급한 키만 이월로 받는다."""
    from devcrew.tutor import issue_quiz
    miss = {"q_key": "k-old-1", "area": "세션",
            "evidence": [{"path": "a.py", "start_line": 5, "end_line": 5, "quote": "line5"}]}
    draft = [_q(0, area="세션", start=5, source_key="k-old-1"),
             _q(1, area="세션", start=6, source_key="지어낸키")]
    draft += [_q(i, area=f"영역{i}", start=i + 10) for i in range(2, 12)]
    author = Scripted([_authored(draft)])
    verifier = Scripted([_verdicts(12)])
    orch, _ = make_orch(tmp_path, author, verifier)
    res = await issue_quiz(orch, load_config(), repo_name="r", repo_path=str(repo),
                           exec_id="E", misses=[miss])
    assert "k-old-1" in author.initial_messages[0] or "k-old-1" in author.sent[0][1]
    carried = [q for q in res.questions if q.carried]
    assert [q.key for q in carried] == ["k-old-1"]           # 지어낸 키는 이월 안 됨


@pytest.mark.asyncio
async def test_select_ten_prioritizes_carried_then_area_diversity():
    from devcrew.quiz import Evidence, Question
    from devcrew.tutor import select_ten

    def q(area, key, carried=False):
        return Question(area=area, type="CORRECT", stem="s", options=list("ABCD"),
                        answer_index=0, evidence=[Evidence("a.py", 1, 1, "x")],
                        explanation="e", key=key, carried=carried)

    pool = [q("세션", f"s{i}") for i in range(9)]
    pool += [q("리포트", "r1"), q("권한", "p1")]
    pool += [q("세션", "old1", carried=True), q("세션", "old2", carried=True)]
    picked = select_ten(pool, count=10)
    assert [x.key for x in picked[:2]] == ["old1", "old2"]      # 오답 유래가 먼저
    assert {"r1", "p1"} <= {x.key for x in picked}              # 영역 다양성이 그다음
    assert len(picked) == 10


@pytest.mark.asyncio
async def test_verifier_failure_does_not_pass_unverified_questions(tmp_path, repo):
    """검증이 죽으면 통과시키지 않는다 — 검증 없는 문항이 사람에게 가면 안 된다."""
    from devcrew.tutor import issue_quiz
    author = Scripted([_authored([_q(i, start=i + 1) for i in range(12)])])
    verifier = Scripted([])
    verifier.fail_after = 0                                    # 검증 세션이 죽는다
    orch, _ = make_orch(tmp_path, author, verifier)
    res = await issue_quiz(orch, load_config(), repo_name="r", repo_path=str(repo),
                           exec_id="E", misses=[])
    assert res.questions == [] and res.shortfall is True
    assert any("검증" in n for n in res.notes)


# ── Codex 리뷰 대응 (#19) ───────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("verdicts,label", [
    ({"status": "PASS", "summary": "s", "verdicts": []}, "빈 판정"),
    ({"status": "PASS", "summary": "s",
      "verdicts": [{"index": 0, "verdict": "PASS", "reason": "ok"}]}, "일부만 판정"),
    ({"status": "BLOCKED", "summary": "repo를 못 읽음",
      "verdicts": [{"index": i, "verdict": "PASS", "reason": "ok"}
                   for i in range(12)]}, "BLOCKED 상태"),
    ({"status": "PASS", "summary": "s",
      "verdicts": [{"index": 0, "verdict": "PASS", "reason": "ok"}] * 12}, "중복 인덱스"),
])
async def test_incomplete_verdicts_pass_nothing(tmp_path, repo, verdicts, label):
    """마지막 관문은 fail-closed다. 판정이 온전하지 않으면 전부 폐기한다 —
    누락된 인덱스를 묵시적 PASS로 읽으면 검증을 우회하는 길이 열린다."""
    from devcrew.tutor import issue_quiz
    author = Scripted([_authored([_q(i, start=i + 1) for i in range(12)])])
    verifier = Scripted([verdicts, verdicts])
    orch, _ = make_orch(tmp_path, author, verifier)
    res = await issue_quiz(orch, load_config(), repo_name="r", repo_path=str(repo),
                           exec_id="E", misses=[])
    assert res.questions == [], label
    assert res.shortfall is True


@pytest.mark.asyncio
async def test_select_ten_caps_carried_and_drops_duplicate_keys():
    """같은 key가 한 회차에 둘 들어오면 하나는 miss, 하나는 clear가 되어
    최종 오답 상태가 문항 순서에 좌우된다."""
    from devcrew.quiz import Evidence, Question
    from devcrew.tutor import MAX_CARRIED, select_ten

    def q(area, key, carried=False):
        return Question(area=area, type="CORRECT", stem="s", options=list("ABCD"),
                        answer_index=0, evidence=[Evidence("a.py", 1, 1, "x")],
                        explanation="e", key=key, carried=carried)

    pool = [q("세션", f"old{i}", carried=True) for i in range(5)]      # 오답 5개
    pool += [q("세션", "old0", carried=True)]                          # 같은 key 중복
    pool += [q(f"영역{i}", f"n{i}") for i in range(9)]
    picked = select_ten(pool, count=10)
    assert sum(1 for x in picked if x.carried) == MAX_CARRIED
    assert len({x.key for x in picked}) == len(picked)                  # key 중복 없음


def test_dedupe_keeps_the_carried_variant_when_keys_collide():
    """같은 key의 일반 문항이 먼저 있어도 오답 유래 쪽이 살아남아야 한다 —
    아니면 '지난 오답을 먼저 낸다'가 조용히 사라진다."""
    from devcrew.quiz import Evidence, Question
    from devcrew.tutor import select_ten

    def q(key, carried):
        return Question(area="세션", type="CORRECT", stem="s", options=list("ABCD"),
                        answer_index=0, evidence=[Evidence("a.py", 1, 1, "x")],
                        explanation="e", key=key, carried=carried)

    picked = select_ten([q("k1", False), q("k1", True)], count=10)
    assert len(picked) == 1 and picked[0].carried is True


@pytest.mark.asyncio
async def test_non_integer_verdict_index_rejects_the_batch(tmp_path, repo):
    """bool·실수 인덱스를 int()로 구부려 받으면 판정이 엉뚱한 문항에 붙는다."""
    from devcrew.tutor import issue_quiz
    author = Scripted([_authored([_q(i, start=i + 1) for i in range(12)])])
    bad = {"status": "PASS", "summary": "s",
           "verdicts": [{"index": True, "verdict": "PASS", "reason": "ok"}]
                       + [{"index": i, "verdict": "PASS", "reason": "ok"}
                          for i in range(1, 12)]}
    verifier = Scripted([bad, bad])
    orch, _ = make_orch(tmp_path, author, verifier)
    res = await issue_quiz(orch, load_config(), repo_name="r", repo_path=str(repo),
                           exec_id="E", misses=[])
    assert res.questions == []


@pytest.mark.asyncio
async def test_spawn_failure_reason_reaches_the_user(tmp_path, repo):
    """출제가 왜 실패했는지 화면에 남아야 한다. 예외를 삼키면 '문항을 못 만들었다'는
    말만 남고 원인은 trace에도 로그에도 없다 (2026-08-21 첫 회차가 그랬다)."""
    from devcrew.tutor import issue_quiz

    class Exploding(Scripted):
        async def start_session(self, inst, initial_message, **kw):
            raise KeyError(inst.role)          # 정책 누락이 이렇게 터졌다

    orch, _ = make_orch(tmp_path, Exploding([]), Scripted([]))
    res = await issue_quiz(orch, load_config(), repo_name="r", repo_path=str(repo),
                           exec_id="E", misses=[])
    assert res.questions == [] and res.shortfall is True
    joined = " ".join(res.notes)
    assert "KeyError" in joined and "TUTOR" in joined


# ── 세션이 매달렸을 때 ────────────────────────────────────────────────────────

class Hanging(Scripted):
    """첫 turn(`start_session`)이 끝나지 않는 provider — 2026-08-24 실사고 재현."""

    async def start_session(self, inst, initial_message, **kw):
        await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_hanging_first_turn_is_bounded_by_the_turn_timeout(tmp_path, repo, monkeypatch):
    """출제의 실제 작업은 **첫 turn**에서 일어난다. 거기에 시간 상한이 없으면 세션이
    영원히 매달리고, `_lock` 뒤의 모든 회차가 조용히 멈춘다 (2026-08-24)."""
    import devcrew.tutor as tutor_mod
    monkeypatch.setattr(tutor_mod, "TURN_TIMEOUT", 0.2)
    orch, _ = make_orch(tmp_path, Hanging([]), Scripted([]))
    # 회귀하면 매달린다. 테스트가 함께 매달리지 않도록 바깥에서도 상한을 건다.
    res = await asyncio.wait_for(
        tutor_mod.issue_quiz(orch, load_config(), repo_name="r",
                             repo_path=str(repo), exec_id="E", misses=[]),
        timeout=10)
    assert res.questions == []
    assert any("TimeoutError" in n for n in res.notes), res.notes


@pytest.mark.asyncio
async def test_hanging_session_does_not_hold_the_pipeline_forever(tmp_path, repo, monkeypatch):
    """상한이 걸리면 파이프라인은 **반환된다** — 다음 회차가 lock을 얻을 수 있어야 한다."""
    import devcrew.tutor as tutor_mod
    monkeypatch.setattr(tutor_mod, "TURN_TIMEOUT", 0.2)
    orch, _ = make_orch(tmp_path, Hanging([]), Scripted([]))
    await asyncio.wait_for(
        tutor_mod.issue_quiz(orch, load_config(), repo_name="r", repo_path=str(repo),
                             exec_id="E", misses=[]),
        timeout=10)


class Exploding(Scripted):
    """첫 turn은 되고 nudge에서 터진다 — 세션은 이미 떴고 sid는 살아 있는 상태."""

    async def send(self, session_id, message):
        raise RuntimeError("provider 폭발")


@pytest.mark.asyncio
async def test_ask_archives_the_worker_on_success(tmp_path, repo):
    """1회용 출제 세션은 반납한다 — `registry.finish`만으로는 프로세스가 안 죽는다.

    2026-08-24: `_ask`가 `finish`만 하고 `archive`를 하지 않아 회차마다 워커가 쌓였다.
    `sid`가 내부 코루틴 지역변수뿐이라 꺼낼 수 없었던 것이 구조적 원인이다.
    """
    from devcrew.tutor import issue_quiz
    author = Scripted([_authored([_q(i, start=i + 1) for i in range(12)])])
    verifier = Scripted([_verdicts(12)])
    orch, _ = make_orch(tmp_path, author, verifier)
    await issue_quiz(orch, load_config(), repo_name="r", repo_path=str(repo),
                     exec_id="E", misses=[])
    assert author.archived and verifier.archived


@pytest.mark.asyncio
async def test_ask_archives_and_records_reason_when_the_turn_fails(tmp_path, repo):
    """실패해도 반납하고, **사유를 trace에 남긴다.**

    사유가 메모리 `notes`에만 있으면 회차가 끝나는 순간 사라진다 — 2026-08-24
    15:52 출제자가 6분 반을 쓰고 0문항을 냈는데 왜인지 어디에도 없었다.
    """
    from devcrew.quiz import AUTHOR_FAILED_EVENT
    from devcrew.tutor import issue_quiz
    author = Exploding([])
    verifier = Scripted([])
    orch, trace = make_orch(tmp_path, author, verifier)
    res = await issue_quiz(orch, load_config(), repo_name="r", repo_path=str(repo),
                           exec_id="E", misses=[])
    assert res.questions == []
    assert author.archived, "실패 경로에서도 워커를 반납해야 한다"
    reasons = [e["payload"]["reason"] for e in trace.events(event_type=AUTHOR_FAILED_EVENT)]
    assert any("provider 폭발" in r for r in reasons), reasons
    assert any("TUTOR" in e["payload"]["role"] for e in trace.events(
        event_type=AUTHOR_FAILED_EVENT))
