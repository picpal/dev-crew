"""quiz_report — 요약 대시보드 + 영역별 카드 HTML. JS 0, 전 출력 escape."""
import pytest

from devcrew.quiz import grade, parse_questions
from devcrew.report.quiz_report import render_quiz_report


def _q(area, ans, stem="다음 중 옳은 것은?", path="a.py", start=1, diagram=None,
       explanation="TTL은 6시간이다"):
    return {"area": area, "type": "CORRECT", "stem": stem,
            "options": ["A", "B", "C", "D"], "answer_index": ans,
            "evidence": [{"path": path, "start_line": start, "end_line": start + 2,
                          "quote": "SESSION_TTL = 3600"}],
            "explanation": explanation, "diagram": diagram}


@pytest.fixture
def card():
    qs = parse_questions([_q("세션", 0, path="a.py", start=1),
                          _q("세션", 1, path="a.py", start=9),
                          _q("리포트", 2, path="b.py", start=1,
                             diagram={"type": "bar", "items": [{"label": "발행", "value": 3}]})])
    return grade(qs, {0: 0, 1: 3, 2: 2})


def _html(card, **kw):
    return render_quiz_report(card, **{"repo": "message-gate", "added": 1, "cleared": 0, **kw})


def test_report_is_self_contained_without_js_or_external_requests(card):
    html = _html(card)
    assert "<script" not in html and "http://" not in html and "https://" not in html
    assert "<style" in html


def test_each_question_is_a_card_with_explanation_behind_details(card):
    html = _html(card)
    assert html.count('<details class="qa"') == card.total
    assert "TTL은 6시간이다" in html


def test_wrong_answers_open_by_default(card):
    """리포트를 여는 이유가 오답인데 전부 접어 두면 열 문항을 일일이 눌러야 한다."""
    html = _html(card)
    assert html.count('<details class="qa" open>') == card.total - card.correct


def test_mostly_wrong_rounds_collapse_every_card():
    """열 중 아홉이 펼쳐져 있으면 펼침은 '여기를 보라'는 뜻을 잃는다."""
    qs = parse_questions([_q("세션", 0), _q("세션", 0, path="b.py"), _q("권한", 0)])
    html = _html(grade(qs, {0: 1, 1: 1, 2: 1}), repo=None)     # 전부 오답
    assert " open>" not in html


def test_single_area_round_has_no_area_chart():
    """막대 하나짜리 차트는 차트가 아니다 — hero가 이미 같은 값을 말한다."""
    qs = parse_questions([_q("세션", 0), _q("세션", 1, path="b.py")])
    html = _html(grade(qs, {0: 0, 1: 1}), repo=None)
    assert "영역별 정답률" not in html and "가장 약한 영역" not in html


def test_cards_are_grouped_by_area_in_issue_order(card):
    html = _html(card)
    assert '<span class="area-name">세션</span>' in html
    assert html.index("area-name\">세션") < html.index("area-name\">리포트")


def test_report_escapes_question_text():
    qs = parse_questions([_q("세션", 0, stem='<img src=x onerror="alert(1)">')])
    html = _html(grade(qs, {0: 0}), repo=None)
    assert "<img" not in html and "&lt;img" in html


def test_inline_markdown_is_rendered_not_shown_raw():
    """모델은 `**굵게**`로 쓴다. 그대로 escape하면 지문에 별표가 찍힌다."""
    qs = parse_questions([_q("세션", 0, stem="다음 중 **틀린** 것은? `quiz.py` 기준")])
    html = _html(grade(qs, {0: 0}), repo=None)
    assert "<strong>틀린</strong>" in html and "<code>quiz.py</code>" in html
    assert "**틀린**" not in html


def test_markdown_conversion_cannot_open_an_injection_path():
    """escape가 먼저다 — 순서가 뒤집히면 모델 출력이 태그가 된다."""
    qs = parse_questions([_q("세션", 0, stem="**<img src=x onerror=alert(1)>**")])
    html = _html(grade(qs, {0: 0}), repo=None)
    assert "<img" not in html and "<strong>&lt;img" in html


def test_report_shows_score_and_note_delta(card):
    html = _html(card, added=2, cleared=1)
    assert "3문항 중 2문항 정답" in html
    assert "+2" in html and "오답 노트 추가" in html and "해소" in html


def test_area_chart_has_a_table_twin_and_names_the_weakest_area(card):
    """막대 길이로만 읽히는 값이 없게 한다 (표 짝), 약한 영역은 하나만 짚는다."""
    html = _html(card)
    assert "표로 보기" in html and "<table" in html
    assert "가장 약한 영역" in html and "세션" in html


def test_report_shows_evidence_location_and_quote(card):
    html = _html(card)
    assert "a.py:1" in html and "SESSION_TTL = 3600" in html


def test_report_renders_diagram_when_present(card):
    html = _html(card)
    assert "chart-row" in html and "발행" in html


def test_unanswered_question_is_marked_as_such():
    qs = parse_questions([_q("세션", 0)])
    html = _html(grade(qs, {}), repo=None)
    assert "미응답" in html


def test_report_defines_both_light_and_dark_palettes():
    """다크는 자동 반전이 아니라 같은 토큰을 갈아끼운 별도 값이다."""
    qs = parse_questions([_q("세션", 0)])
    html = _html(grade(qs, {0: 0}), repo=None)
    assert "prefers-color-scheme:dark" in html and "--ink-1:#0b0b0b" in html


def test_report_is_deterministic_so_publishing_stays_idempotent(card):
    """발행은 content hash로 멱등이다 — 렌더마다 값이 바뀌면 같은 회차가 매번 새 객체."""
    assert _html(card) == _html(card)


def test_headline_does_not_claim_a_miss_on_a_perfect_round():
    """제목이 늘 '무엇을 놓쳤나'면 만점 회차에서 거짓말이 된다."""
    qs = parse_questions([_q("세션", 0), _q("권한", 0)])
    html = _html(grade(qs, {0: 0, 1: 0}), repo=None, added=0, cleared=2)
    assert "무엇을 놓쳤나" not in html and "전부 맞혔습니다" in html
    assert "영역별 정답률" not in html          # 100% 막대만 늘어놓을 이유가 없다


def test_document_quotes_are_not_set_in_monospace():
    """문서 인용은 산문이다 — 한글을 고정폭으로 깔면 읽기가 나빠진다."""
    qs = parse_questions([_q("세션", 0, path="DESIGN.md"), _q("권한", 0, path="a.py")])
    html = _html(grade(qs, {0: 0, 1: 0}), repo=None)
    assert html.count('evi-quote is-prose') == 1 and html.count('"evi-quote"') == 1
