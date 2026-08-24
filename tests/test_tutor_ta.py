"""TUTOR_TA — 회차 컨텍스트 조립과 답변 정리 (순수 함수)."""
from devcrew.quiz import Evidence, Question


def _q(i, *, area="영역", answer_index=0, path="a.py", line=1):
    return Question(area=area, type="CORRECT", stem=f"문항 {i}?",
                    options=["A", "B", "C", "D"], answer_index=answer_index,
                    evidence=[Evidence(path=path, start_line=line, end_line=line,
                                       quote=f"line{line}")],
                    explanation=f"해설 {i}")


def test_round_context_puts_wrong_answers_first():
    """질문은 틀린 문항에서 나온다 — 그걸 앞에 놓는다."""
    from devcrew.tutor_ta import round_context

    qs = [_q(0), _q(1), _q(2)]
    ctx = round_context(qs, {0: 0, 1: 3, 2: 0}, repo_name="myrepo")   # 1번만 오답
    assert ctx.index("문항 1?") < ctx.index("문항 0?")


def test_round_context_carries_answer_evidence_and_choice():
    """정답·근거·해설·학습자의 선택이 모두 들어간다 — 채점 후이므로 감출 것이 없다."""
    from devcrew.tutor_ta import round_context

    ctx = round_context([_q(0, answer_index=2)], {0: 1}, repo_name="myrepo")
    assert "myrepo" in ctx
    assert "해설 0" in ctx and "a.py:1-1" in ctx
    # 보기 목록에 이미 A) B) C) D)가 있으므로 낱글자 존재는 아무것도 증명하지 않는다.
    # 라벨이 붙은 줄 자체를 본다.
    assert "정답: C" in ctx and "학습자 선택: B" in ctx
    assert "(오답)" in ctx


def test_round_context_marks_unanswered():
    from devcrew.tutor_ta import round_context

    ctx = round_context([_q(0)], {}, repo_name="r")
    assert "미응답" in ctx


def test_parse_citations_ignores_malformed_entries():
    """모델 출력은 신뢰하지 않는다 — 형태가 틀린 항목은 조용히 버린다."""
    from devcrew.tutor_ta import parse_citations

    out = parse_citations([
        {"path": "a.py", "start_line": 1, "end_line": 2, "quote": "q"},
        {"path": "b.py", "start_line": "둘", "end_line": 2, "quote": "q"},
        "문자열",
        None,
    ])
    assert len(out) == 1 and out[0].path == "a.py"


def test_clean_answer_drops_only_the_bad_citation(tmp_path):
    """답변 폐기가 아니다 — 틀린 인용만 떼고 본문은 낸다."""
    from devcrew.tutor_ta import clean_answer

    (tmp_path / "a.py").write_text("line1\nline2\n")
    real = Evidence(path="a.py", start_line=1, end_line=1, quote="line1")
    fake = Evidence(path="a.py", start_line=1, end_line=1, quote="지어낸 문장")

    text, kept, dropped = clean_answer("본문", [real, fake], str(tmp_path))

    assert kept == [real] and dropped == 1
    assert text.startswith("본문")
    assert "대조에 실패" in text        # 뗐으면 화면에 적는다 (lessons C12)


def test_clean_answer_says_nothing_when_every_citation_holds(tmp_path):
    from devcrew.tutor_ta import clean_answer

    (tmp_path / "a.py").write_text("line1\n")
    real = Evidence(path="a.py", start_line=1, end_line=1, quote="line1")
    text, kept, dropped = clean_answer("본문", [real], str(tmp_path))
    assert text == "본문" and kept == [real] and dropped == 0


def test_clean_answer_truncates_and_says_so(tmp_path):
    from devcrew.tutor_ta import ANSWER_LIMIT, clean_answer

    text, _, _ = clean_answer("가" * (ANSWER_LIMIT + 500), [], str(tmp_path))
    assert len(text) < ANSWER_LIMIT + 200
    assert "잘렸습니다" in text


def test_round_context_treats_out_of_range_choice_as_unanswered():
    """범위 밖 선택은 미응답으로 처리한다 — 거짓말 선택지를 렌더링하지 않는다."""
    from devcrew.tutor_ta import round_context

    # 보기는 0-3 (A-D)인데 99를 받으면
    ctx = round_context([_q(0)], {0: 99}, repo_name="r")
    assert "미응답" in ctx
    # 존재하지 않는 선택지를 렌더링하지 않는다
    assert "학습자 선택: " in ctx and "학습자 선택: E" not in ctx

    # 음수도 마찬가지
    ctx = round_context([_q(0)], {0: -1}, repo_name="r")
    assert "미응답" in ctx


def test_clean_answer_shows_disclosure_even_when_truncated(tmp_path):
    """긴 본문 + 버린 인용이 함께 있을 때도 공개를 숨기지 않는다 (회귀 테스트).

    잘림 처리 후 인용 공개를 붙여야 순서 흔들려도 메시지가 손실되지 않는다.
    """
    from devcrew.tutor_ta import ANSWER_LIMIT, clean_answer

    (tmp_path / "a.py").write_text("line1\n")
    real = Evidence(path="a.py", start_line=1, end_line=1, quote="line1")
    fake = Evidence(path="a.py", start_line=1, end_line=1, quote="지어낸")

    text, _, dropped = clean_answer("X" * (ANSWER_LIMIT + 100), [real, fake], str(tmp_path))

    # 본문이 잘렸고
    assert "잘렸습니다" in text
    # 인용 공개도 있어야 한다 (dropped > 0이므로)
    assert dropped == 1 and "대조에 실패" in text
