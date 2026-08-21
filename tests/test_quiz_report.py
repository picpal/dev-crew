"""quiz_report — 영역별 카드 HTML. JS 0, 전 출력 escape."""
import pytest

from devcrew.quiz import grade, parse_questions
from devcrew.report.quiz_report import render_quiz_report


def _q(area, ans, stem="다음 중 옳은 것은?", path="a.py", start=1, diagram=None):
    return {"area": area, "type": "CORRECT", "stem": stem,
            "options": ["A", "B", "C", "D"], "answer_index": ans,
            "evidence": [{"path": path, "start_line": start, "end_line": start + 2,
                          "quote": "SESSION_TTL = 3600"}],
            "explanation": "TTL은 6시간이다", "diagram": diagram}


@pytest.fixture
def card():
    qs = parse_questions([_q("세션", 0, path="a.py", start=1),
                          _q("세션", 1, path="a.py", start=9),
                          _q("리포트", 2, path="b.py", start=1,
                             diagram={"type": "bar", "items": [{"label": "발행", "value": 3}]})])
    return grade(qs, {0: 0, 1: 3, 2: 2})


def test_report_is_self_contained_without_js_or_external_requests(card):
    html = render_quiz_report(card, repo="message-gate", added=1, cleared=0)
    assert "<script" not in html and "http://" not in html and "https://" not in html
    assert "<style" in html


def test_each_question_is_a_card_with_explanation_behind_details(card):
    html = render_quiz_report(card, repo="message-gate", added=1, cleared=0)
    assert html.count("<details") == card.total
    assert "TTL은 6시간이다" in html


def test_cards_are_grouped_by_area(card):
    html = render_quiz_report(card, repo="message-gate", added=1, cleared=0)
    assert "<h2>세션" in html and "<h2>리포트" in html
    assert html.index("<h2>세션") < html.index("<h2>리포트")   # 출제 순서대로 묶인다


def test_report_escapes_question_text():
    qs = parse_questions([_q("세션", 0, stem='<img src=x onerror="alert(1)">')])
    html = render_quiz_report(grade(qs, {0: 0}), repo=None, added=0, cleared=0)
    assert "<img" not in html and "&lt;img" in html


def test_report_shows_score_and_note_delta(card):
    html = render_quiz_report(card, repo="message-gate", added=2, cleared=1)
    assert "2 / 3" in html
    assert "오답 +2" in html and "해소 1" in html


def test_report_shows_evidence_location(card):
    html = render_quiz_report(card, repo="message-gate", added=1, cleared=0)
    assert "a.py:1" in html


def test_report_renders_diagram_when_present(card):
    html = render_quiz_report(card, repo="message-gate", added=1, cleared=0)
    assert "<svg" in html and "발행" in html


def test_unanswered_question_is_marked_as_such(card):
    qs = parse_questions([_q("세션", 0)])
    html = render_quiz_report(grade(qs, {}), repo=None, added=1, cleared=0)
    assert "미응답" in html
