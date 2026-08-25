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
    """2초에 한 칸 — **CSS keyframes로 선다.**

    스크립트는 재생/일시정지·키보드를 얹는 progressive enhancement일 뿐이라, CSP에
    막히거나 실패해도 자동 진행과 라인 클릭은 그대로 돌아야 한다. 그래서 애니메이션
    규칙이 CSS 안에 온전히 있는지를 본다 (스크립트 유무가 아니라).
    """
    from devcrew.report.code_report import STEP_SECONDS, render_code_report

    html = render_code_report(_trace(4), question="q")
    assert "@keyframes win" in html and f"{STEP_SECONDS}s" in html
    assert ".hl{animation:win var(--total) linear infinite" in html
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


def test_worker_csp_matches_the_script_hash():
    """worker의 `script-src 'sha256-…'`와 실제 스크립트가 같아야 한다.

    어긋나면 브라우저가 스크립트를 **조용히** 차단한다 — 페이지는 뜨고 CSS 재생도 도는데
    재생/이전/다음 버튼만 아무 반응이 없다. 그 증상으로는 원인을 못 찾는다.
    JS_SRC를 한 글자라도 고치면 이 테스트가 먼저 깨져서 worker도 같이 고치게 만든다.
    """
    from pathlib import Path

    from devcrew.report.code_report import script_hash

    worker = Path(__file__).resolve().parents[1] / "worker" / "src" / "index.js"
    assert f'"{script_hash()}"' in worker.read_text(), (
        f"worker의 CODE_SCRIPT_HASH를 {script_hash()} 로 고치고 재배포해야 한다")


def test_page_script_is_exactly_the_hashed_source():
    """페이지에 박히는 것도 그 원본 그대로여야 한다 — 공백 한 칸이면 해시가 달라진다."""
    import base64
    import hashlib
    import re

    from devcrew.report.code_report import render_code_report, script_hash

    html = render_code_report(_trace(3), question="q")
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    assert m, "스크립트가 없다"
    got = "sha256-" + base64.b64encode(
        hashlib.sha256(m.group(1).encode()).digest()).decode()
    assert got == script_hash()


def test_controls_degrade_without_javascript():
    """스크립트가 막혀도 페이지는 완결이어야 한다.

    JS 컨트롤은 `.jsonly`라 기본이 `display:none`이고, 스크립트가 `.js`를 달아야만
    나타난다. 반대로 CSS 전용 '처음부터'는 `.nojs`라 JS가 붙으면 사라진다.
    """
    from devcrew.report.code_report import render_code_report

    html = render_code_report(_trace(3), question="q")
    assert ".jsonly{display:none}" in html and ".js .jsonly{display:inline-flex}" in html
    assert 'class="replay nojs"' in html and ".js .nojs{display:none}" in html
    assert 'data-act="play"' in html and 'data-act="next"' in html


def test_scroll_mechanisms_never_run_together():
    """세로 이동은 **모드마다 하나씩만** 돈다 (2026-08-25 회귀).

    `overflow:auto`(진짜 스크롤)와 `translateY`(transform)를 같이 켜면, 콘텐츠는
    transform으로 올라가 있는데 스크롤 컨테이너는 그걸 모른다 — 사용자가 위로 올리면
    scrollTop 0에서 멈추는데 화면은 여전히 아래쪽이라 **위로 되돌아갈 수 없다.**
    라인을 클릭하면 의도보다 더 내려가 클릭한 줄이 가려지는 것도 같은 원인이다.
    """
    from devcrew.report.code_report import render_code_report

    html = render_code_report(_trace(3), question="q")
    # JS 없음 = transform으로 따라가므로 컨테이너는 스크롤하지 않는다
    assert ".code{--lh:1.55rem;position:relative;overflow:hidden" in html
    # JS 있음 = 진짜 스크롤, transform은 끈다
    assert ".js .code{overflow:auto" in html and ".js .track{transform:none}" in html
    # 수동 선택의 transform 규칙은 no-JS 전용이어야 한다
    assert "html:not(.js) #st0:checked~.stage .track{transform:" in html
    assert "\n#st0:checked~.stage .track{transform:" not in html


def test_control_glyphs_are_not_mangled_by_python_escapes():
    """CSS `content:"\\25b6"` 을 파이썬 문자열에 그대로 쓰면 `\\25` 가 8진 이스케이프로
    먹혀 화면에 "b6" 이 찍힌다 — 실제로 재생 버튼이 `b6`, 변경 표시가 `90` 이었다."""
    from devcrew.report.code_report import _CSS

    assert '"▶"' in _CSS and '"⏸"' in _CSS and '" ←"' in _CSS
    assert "\x15" not in _CSS and "\x11" not in _CSS and "\x13" not in _CSS


def test_code_pane_does_not_use_smooth_scrolling():
    """`scroll-behavior:smooth`면 스크립트가 거는 스크롤이 적용되지 않는다 (실측 2026-08-25).

    scrollTop 대입도, `scrollTo({behavior:'smooth'})`도 최종값이 0으로 남았다 — 그래서
    라인을 클릭해도 그 줄로 안 갔다. 2초에 한 칸이라 즉시 이동으로 충분하다.
    """
    import re

    from devcrew.report.code_report import _CSS

    rules = re.sub(r"/\*.*?\*/", "", _CSS, flags=re.S)   # 주석의 설명까지 잡지 않는다
    assert "scroll-behavior" not in rules
