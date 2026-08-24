"""코드 실행 리포트 렌더 — 단일 파일, JS 없이 도는 애니메이션.

`worker/src/index.js`의 CSP는 `default-src 'none'; style-src 'unsafe-inline'`이라
**스크립트가 실행되지 않는다.** 그래서 자동 진행·수동 스텝이 전부 CSS로 서 있어야
한다 — JS는 나중에 얹는 progressive enhancement일 뿐, 없어도 페이지가 완결이어야 한다.
"""
import re

import pytest

from devcrew.tutor_code import Line, Step, Trace, Var


def _trace(n=3):
    return Trace(
        title="appsec restart — 정지부터 평문 파기까지",
        role_of_code="WAS를 안전하게 다시 띄운다. 없으면 평문이 디스크에 남는다.",
        path="deploy/bin/appsec-lib.sh",
        lines=[Line(number=100 + i, text=f"  line {i} <b>&amp;</b>") for i in range(n + 2)],
        steps=[Step(line=100 + i, reason=f"이유 {i}",
                    vars=[Var(name="waited", value=str(i), changed=bool(i)),
                          Var(name="url", value="https://x/health", changed=False)])
               for i in range(n)])


def test_page_is_self_contained_and_escapes_everything():
    from devcrew.report.code_report import render_code_report

    html = render_code_report(_trace(), question="<img src=x onerror=alert(1)>")
    assert html.startswith("<!doctype html>") and html.rstrip().endswith("</html>")
    # 요점은 `<`가 막혀 태그가 될 수 없다는 것이다. `onerror=…`라는 **글자**는 남지만
    # 이스케이프된 텍스트라 아무것도 실행하지 않는다 — 거기까지 지우면 질문 원문이
    # 화면에서 왜곡된다.
    assert "<img" not in html and "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "<b>&amp;</b>" not in html          # 코드 원문도 이스케이프된다
    assert 'src="http' not in html and "href=\"http" not in html   # 외부 참조 없음


def test_animation_runs_without_javascript():
    """2초에 한 칸 — CSS keyframes로 선다. `<script>`가 없어야 한다."""
    from devcrew.report.code_report import STEP_SECONDS, render_code_report

    html = render_code_report(_trace(4), question="q")
    assert "<script" not in html.lower()
    assert "@keyframes" in html and f"{STEP_SECONDS}s" in html
    # 스텝 n은 n*2초에 켜진다
    assert "animation-delay" in html


def test_manual_stepping_is_css_only():
    """손으로 넘길 수 있어야 한다 — 라디오 + :checked. 누르면 자동재생이 멈춘다."""
    from devcrew.report.code_report import render_code_report

    html = render_code_report(_trace(3), question="q")
    assert html.count('type="radio"') >= 3
    assert ":checked" in html and ":has(" in html


def test_every_step_shows_its_line_reason_and_vars():
    from devcrew.report.code_report import render_code_report

    html = render_code_report(_trace(3), question="q")
    for i in range(3):
        assert f"이유 {i}" in html
    assert "waited" in html and "https://x/health" in html
    assert "is-changed" in html, "바뀐 값 강조가 없다"


def test_dropped_steps_are_disclosed():
    """버린 스텝을 조용히 숨기지 않는다 (lessons C12)."""
    from devcrew.report.code_report import render_code_report

    t = _trace(3)
    t.dropped = 2
    assert "2" in re.sub(r"[^0-9가-힣]", "", render_code_report(t, question="q"))
    assert "제외" in render_code_report(t, question="q")


def test_role_of_code_and_title_are_shown():
    """'이 코드의 역할은 무엇인가'가 이 리포트의 목적이다 — 스텝보다 먼저 보여야 한다."""
    from devcrew.report.code_report import render_code_report

    html = render_code_report(_trace(2), question="q")
    assert "정지부터 평문 파기까지" in html
    assert "평문이 디스크에 남는다" in html
    body = html[html.index("평문이 디스크에 남는다"):]
    assert "이유 0" in body, "역할 설명이 스텝보다 뒤에 있다"
