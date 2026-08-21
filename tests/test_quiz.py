"""quiz — 문항 모델·q_key·인용 대조·채점·오답 노트 (LLM 없음, 순수 로직)."""
import pytest


def _ev(path="src/a.py", start=10, end=20, quote="SESSION_TTL = 3600"):
    return {"path": path, "start_line": start, "end_line": end, "quote": quote}


def _q(area="세션", type="CORRECT", stem="다음 중 옳은 것은?",
       options=None, answer_index=0, evidence=None, explanation="해설",
       diagram=None, **ev):
    return {"area": area, "type": type, "stem": stem,
            "options": options if options is not None else ["A", "B", "C", "D"],
            "answer_index": answer_index,
            "evidence": [_ev(**ev)] if evidence is None else evidence,
            "explanation": explanation, "diagram": diagram}


# ── Task 2: 문항 모델과 q_key ───────────────────────────────────────────────
def test_q_key_follows_evidence_location_not_wording():
    """지문을 바꿔 물어도 같은 지점이면 같은 문항이다 — 오답 해소가 그 위에 선다."""
    from devcrew.quiz import Evidence, q_key
    e1 = [Evidence("src/a.py", 10, 20, "x")]
    e2 = [Evidence("src/a.py", 10, 25, "전혀 다른 인용")]   # 같은 시작 줄
    e3 = [Evidence("src/b.py", 10, 20, "x")]
    assert q_key("세션", e1) == q_key("세션", e2)
    assert q_key("세션", e1) != q_key("세션", e3)
    assert q_key("세션", e1) != q_key("리포트", e1)


def test_q_key_is_order_independent():
    from devcrew.quiz import Evidence, q_key
    a, b = Evidence("src/a.py", 1, 2, "x"), Evidence("src/b.py", 3, 4, "y")
    assert q_key("세션", [a, b]) == q_key("세션", [b, a])


def test_carried_key_survives_reissue():
    """재출제 문항은 원래 키를 유지한다 — 인용 줄이 흔들려도 오답이 해소된다."""
    from devcrew.quiz import parse_questions
    q = parse_questions([_q(start=38)], carried_keys={0: "deadbeef1234"})[0]
    assert q.key == "deadbeef1234"
    assert q.carried is True


def test_parse_questions_drops_malformed():
    from devcrew.quiz import parse_questions
    assert parse_questions([{"area": "x"}]) == []                   # 필드 누락
    assert parse_questions([_q(options=["A", "B"])]) == []           # 보기 4개 아님
    assert parse_questions([_q(answer_index=9)]) == []               # 범위 밖
    assert parse_questions([_q(type="MAYBE")]) == []                 # 유형 이상
    assert parse_questions([_q(evidence=[{"path": "a"}])]) == []     # evidence 형태 이상
    assert len(parse_questions([_q()])) == 1


def test_parse_questions_keeps_empty_evidence_for_the_gate_to_drop():
    """근거 없음은 파싱 단계가 아니라 인용 대조가 사유와 함께 버린다."""
    from devcrew.quiz import parse_questions
    qs = parse_questions([_q(evidence=[])])
    assert len(qs) == 1 and qs[0].evidence == []


# ── Task 3: 인용 대조 (결정적 검증) ─────────────────────────────────────────
def test_citation_must_exist_within_cited_lines(tmp_path):
    """지어낸 근거를 LLM 판단 없이 버린다 — 이 게이트가 이 Agent의 1차 방어다."""
    from devcrew.quiz import parse_questions, verify_citations
    (tmp_path / "a.py").write_text("l1\nl2\nSESSION_TTL = 3600\nl4\n")
    ok, dropped = verify_citations(parse_questions([
        _q(path="a.py", start=3, end=3, quote="SESSION_TTL = 3600"),
        _q(path="a.py", start=1, end=2, quote="SESSION_TTL = 3600"),   # 범위 밖
        _q(path="nope.py", start=1, end=1, quote="x"),                 # 없는 파일
        _q(path="../../etc/passwd", start=1, end=1, quote="root"),     # 탈출
        _q(evidence=[]),                                               # 근거 없음
    ]), tmp_path)
    assert len(ok) == 1 and len(dropped) == 4
    assert {r for _, r in dropped} == {"quote-not-in-range", "path-missing",
                                       "path-outside-repo", "no-evidence"}


def test_citation_match_normalizes_whitespace(tmp_path):
    """모델은 줄바꿈·들여쓰기를 흘린다. 공백 차이로 진짜 근거를 버리면 안 된다."""
    from devcrew.quiz import parse_questions, verify_citations
    (tmp_path / "a.py").write_text("def  f(x):\n    return   x\n")
    ok, _ = verify_citations(parse_questions([
        _q(path="a.py", start=1, end=2, quote="def f(x): return x")]), tmp_path)
    assert len(ok) == 1


def test_absolute_path_is_rejected(tmp_path):
    from devcrew.quiz import parse_questions, verify_citations
    (tmp_path / "a.py").write_text("x\n")
    _, dropped = verify_citations(parse_questions([
        _q(path=str(tmp_path / "a.py"), start=1, end=1, quote="x")]), tmp_path)
    assert dropped[0][1] == "path-outside-repo"


def test_every_evidence_item_must_hold(tmp_path):
    """근거가 여러 개면 전부 실재해야 한다 — 하나만 진짜면 나머지는 장식이다."""
    from devcrew.quiz import parse_questions, verify_citations
    (tmp_path / "a.py").write_text("real line\n")
    _, dropped = verify_citations(parse_questions([
        _q(evidence=[_ev(path="a.py", start=1, end=1, quote="real line"),
                     _ev(path="a.py", start=1, end=1, quote="지어낸 문장")])]), tmp_path)
    assert dropped[0][1] == "quote-not-in-range"


def test_binary_or_unreadable_file_is_dropped_not_raised(tmp_path):
    from devcrew.quiz import parse_questions, verify_citations
    (tmp_path / "a.bin").write_bytes(b"\xff\xfe\x00\x01")
    _, dropped = verify_citations(parse_questions([
        _q(path="a.bin", start=1, end=1, quote="x")]), tmp_path)
    assert dropped[0][1] in ("path-unreadable", "quote-not-in-range")


# ── Task 4: 채점 ────────────────────────────────────────────────────────────
def test_grade_counts_by_area_and_treats_unanswered_as_wrong():
    from devcrew.quiz import grade, parse_questions
    qs = parse_questions([_q(area="세션", answer_index=0, path="a.py", start=1),
                          _q(area="세션", answer_index=1, path="a.py", start=2),
                          _q(area="리포트", answer_index=2, path="b.py", start=1)])
    card = grade(qs, {0: 0, 1: 3})                 # 2번 문항 미응답
    assert (card.correct, card.total) == (1, 3)
    assert card.by_area == {"세션": (1, 2), "리포트": (0, 1)}
    assert card.results[2].choice is None and card.results[2].correct is False
    assert card.results[0].correct is True


def test_grade_area_percentages():
    from devcrew.quiz import grade, parse_questions
    qs = parse_questions([_q(area="세션", answer_index=0, path="a.py", start=1),
                          _q(area="세션", answer_index=0, path="a.py", start=2)])
    card = grade(qs, {0: 0, 1: 1})
    assert card.area_pct() == [("세션", 50.0)]


# ── Task 5: 오답 노트 ───────────────────────────────────────────────────────
def test_miss_clear_miss_leaves_the_miss_open(tmp_path):
    """해소 후 다시 틀리면 다시 열린다 — 벽시계가 아니라 rowid 순서로 판정한다."""
    from devcrew.quiz import open_misses
    from devcrew.store.trace import TraceStore
    t = TraceStore(tmp_path / "t.db")
    for et in ("QuizMissEvent", "QuizClearedEvent", "QuizMissEvent"):
        t.append(et, task_id="T", execution_id="E", payload={"q_key": "k1", "area": "세션"})
    assert [m["q_key"] for m in open_misses(t, "E")] == ["k1"]


def test_cleared_after_miss_removes_it(tmp_path):
    from devcrew.quiz import open_misses
    from devcrew.store.trace import TraceStore
    t = TraceStore(tmp_path / "t.db")
    t.append("QuizMissEvent", task_id="T", execution_id="E", payload={"q_key": "k1"})
    t.append("QuizMissEvent", task_id="T", execution_id="E", payload={"q_key": "k2"})
    t.append("QuizClearedEvent", task_id="T", execution_id="E", payload={"q_key": "k1"})
    assert [m["q_key"] for m in open_misses(t, "E")] == ["k2"]


def test_note_id_is_per_user_and_repo():
    from devcrew.quiz import note_id
    assert note_id("U1", "message-gate") == "TUTOR-U1-message-gate"
    assert note_id("U1", "a") != note_id("U2", "a")


def test_record_scorecard_writes_miss_and_clears_only_prior_keys(tmp_path):
    """맞힌 문항 중 **오답 노트에 있던 것만** 해소로 기록한다."""
    from devcrew.quiz import grade, open_misses, parse_questions, record_scorecard
    from devcrew.store.trace import TraceStore
    t = TraceStore(tmp_path / "t.db")
    qs = parse_questions([_q(area="세션", answer_index=0, path="a.py", start=1),
                          _q(area="세션", answer_index=0, path="a.py", start=2),
                          _q(area="리포트", answer_index=0, path="b.py", start=1)])
    t.append("QuizMissEvent", task_id="T", execution_id="E",
             payload={"q_key": qs[0].key, "area": "세션"})
    card = grade(qs, {0: 0, 1: 1, 2: 2})          # 0번 정답(이전 오답), 1·2번 오답
    added, cleared = record_scorecard(t, "E", card, prior_keys={qs[0].key})
    assert (added, cleared) == (2, 1)
    assert sorted(m["q_key"] for m in open_misses(t, "E")) == sorted([qs[1].key, qs[2].key])
