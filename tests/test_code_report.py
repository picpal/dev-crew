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


def test_header_renders_markdown_and_collapses_long_text():
    """상단 역할 설명이 **날문자**로 찍히고 벽처럼 길면 아무도 안 읽는다 (사용자 2026-08-25).

    - 모델은 `**굵게**`와 백틱을 쓴다 — 렌더해야 한다 (escape가 먼저)
    - 길면 첫 문장만 보이고 나머지는 접는다
    """
    from devcrew.report.code_report import render_code_report

    t = _trace(2)
    t.role_of_code = ("이 메서드는 **유일한 지점**이다. " + "뒤에 아주 긴 설명이 이어진다. " * 20)
    html = render_code_report(t, question="q")
    assert "<strong>유일한 지점</strong>" in html and "**유일한" not in html
    assert "<details" in html and "<summary" in html
    assert "이 메서드는 <strong>유일한 지점</strong>이다." in html


def test_step_reason_renders_inline_code():
    """스텝 이유에도 백틱이 온다 — `health_reachable` 이 날문자로 찍히면 안 된다."""
    from devcrew.report.code_report import render_code_report

    t = _trace(2)
    t.steps[0].reason = "`health_reachable` 은 200이 아니라 응답 여부만 본다"
    html = render_code_report(t, question="q")
    assert "<code>health_reachable</code>" in html


def test_code_line_with_a_step_is_clickable():
    """상단 숫자 버튼 대신 **코드 줄을 눌러** 그 줄의 설명을 본다 (사용자 2026-08-25).

    라벨이 라디오를 켜므로 JS가 필요 없다. 스텝이 없는 줄은 라벨이 아니다 —
    눌러도 아무 일이 없는 것을 누를 수 있게 보이면 안 된다.
    """
    from devcrew.report.code_report import render_code_report

    html = render_code_report(_trace(2), question="q")   # 스텝은 100,101 / 코드는 100~103
    assert '<label class="row is-step" for="st0"' in html
    assert html.count("<label") >= 2
    # 스텝이 없는 줄(102,103)은 라벨이 아니다
    body = html[html.index('<div class="track">'):]
    assert '<div class="row"' in body


def test_a_line_visited_twice_offers_both_visits():
    """루프는 같은 줄을 여러 번 지난다 — 라벨 하나로는 한 회차밖에 못 가리킨다.
    패널에서 다른 회차로 건너뛸 수 있어야 한다."""
    from devcrew.tutor_code import Step, Var
    from devcrew.report.code_report import render_code_report

    t = _trace(2)
    t.steps = [Step(line=100, reason="첫 진입", vars=[]),
               Step(line=101, reason="본문", vars=[]),
               Step(line=100, reason="루프 재진입", vars=[Var("i", "1", True)])]
    html = render_code_report(t, question="q")
    assert "이 줄은 2번" in html
    # 1회차 패널에서 3번 스텝(같은 줄 2회차)으로 갈 수 있어야 한다
    assert 'for="st2"' in html


def test_replay_control_does_not_count_as_manual_stop():
    """'처음부터' 라디오는 `.rt`가 아니어야 한다 — `.rt:checked`면 자동재생이 꺼진다."""
    import re
    from devcrew.report.code_report import render_code_report

    html = render_code_report(_trace(3), question="q")
    m = re.search(r'<input[^>]*id="stAuto"[^>]*>', html)
    assert m and 'class="rt"' not in m.group(0), m.group(0) if m else "재생 컨트롤이 없다"
    assert "처음부터" in html


def test_no_escaped_quotes_leak_into_attributes():
    """`class=&quot;x&quot;`처럼 f-string 안에서 따옴표를 피하려다 속성이 깨진 적이 있다 —
    브라우저는 그걸 값에 따옴표가 든 클래스로 읽어 스타일이 통째로 안 붙는다."""
    from devcrew.report.code_report import render_code_report

    import re

    t = _trace(2)
    t.role_of_code = "첫 문장이다. " + "긴 나머지. " * 40
    t.lines[0].text = '  url="$(cfg was.health.url)"'   # 코드 원문의 따옴표는 정상이다
    html = render_code_report(t, question="q")
    bad = re.findall(r"<[a-zA-Z]+\s+[a-zA-Z-]+=&quot;", html)
    assert not bad, f"태그 속성 자리에 이스케이프된 따옴표: {bad}"
    assert '<p class="rest">' in html
    assert "url=&quot;$(cfg" in html, "코드 원문의 따옴표까지 지우면 안 된다"
