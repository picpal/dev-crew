from devcrew.report.renderer import render


def make_view():
    return {
        "task_id": "TASK-1", "status": "DONE", "lead_time_min": 12,
        "instances": [{"instance_id": "DEV-1", "role": "DEVELOPER",
                       "model": "claude-sonnet-5", "effort": "high"}],
        "loops": [{"iteration": 1, "verdict": "PASS"}],
        "decisions": ["<script>alert(1)</script>주입 시도"],
    }


def test_render_is_self_contained():
    html = render(make_view())
    assert "<html" in html and "TASK-1" in html
    # 외부 요청 0: http(s) 소스 참조가 없어야 한다
    assert "src=\"http" not in html and "href=\"http" not in html
    assert "@import" not in html


def test_render_escapes_agent_output():
    html = render(make_view())
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_records_template_version():
    assert 'data-template-version="' in render(make_view())


def make_hard_view():
    """실물로 봤을 때 깨졌던 입력 — 안 끊기는 긴 토큰, 긴 status, 빈 목록의 반대편."""
    long_id = "DEVELOPER-instance-0f3a9c7d5e1b4a2c8f6e0d3b7a91c4e5f2b8d6a0c3e7f1b9d"
    return {
        "task_id": "TASK-2026-08-21-e2e-delivery-verification-run-0042",
        "status": "NOT_PASS · leader가 LOOP_GUARD_EXCEEDED로 ESCALATE_MODEL을 선택한 뒤 대기 중",
        "lead_time_min": 41,
        "instances": [{"instance_id": f"{long_id}-{n:02d}", "role": "품질 보증 담당자",
                       "model": "claude-opus-5-20260514-thinking-extended-1m-preview",
                       "effort": "medium"} for n in range(20)],
        "loops": [{"iteration": n + 1, "verdict": "NOT_PASS"} for n in range(15)],
        "decisions": ["같은 지적이 3회 반복되어 REPLAN을 선택했다." for _ in range(30)],
    }


def make_empty_view():
    return {"task_id": "T", "status": "DONE",
            "instances": [], "loops": [], "decisions": []}


def test_report_declares_a_mobile_viewport():
    """viewport meta가 없으면 모바일 브라우저가 980px 레이아웃을 그린 뒤 축소한다 —
    글자가 읽을 수 없이 작아진다. 원본 템플릿에 이 태그가 없었다."""
    html = render(make_hard_view())
    assert '<meta name="viewport" content="width=device-width,initial-scale=1">' in html


def test_every_table_sits_in_a_horizontal_scroll_container():
    """375px에서 문서 폭이 657px까지 밀렸다(실측). 원인은 안 끊기는 50자
    instance_id를 담은 표가 스크롤 컨테이너 없이 본문에 그대로 놓인 것."""
    html = render(make_hard_view())
    assert "overflow-x:auto" in html
    # `<table`이 나올 때마다 바로 앞이 table-wrap이어야 한다
    head, *rest = html.split("<table")
    assert rest, "표가 렌더되지 않았다"
    prefix = head
    for part in rest:
        assert prefix.rstrip().endswith("</div>") or 'class="table-wrap"' in \
            prefix[prefix.rfind("<div"):], "표가 table-wrap 밖에 있다"
        prefix = part


def test_scroll_containers_are_reachable_by_keyboard_and_named():
    """좁은 화면에서는 열이 통째로 화면 밖에 있고 스크롤바는 20행짜리 표 맨 아래에
    있다 — 사용자가 열이 잘렸다는 사실 자체를 모른다 (Codex 화면 검토 2026-08-21)."""
    html = render(make_hard_view())
    assert html.count('class="table-wrap" tabindex="0" role="region" aria-label=') == 2
    assert "옆으로 밀어" in html


def test_cells_with_long_values_are_allowed_to_wrap():
    """`overflow-wrap:anywhere`가 없으면 instance_id 한 줄이 표 폭을 밀어낸다."""
    html = render(make_hard_view())
    assert "overflow-wrap:anywhere" in html


def test_report_defines_both_light_and_dark_palettes():
    """배경이 `#fff`로 박혀 있어 다크 OS에서 흰 판때기였다. 다크는 자동 반전이
    아니라 같은 토큰의 별도 값이어야 한다."""
    html = render(make_hard_view())
    assert '<meta name="color-scheme" content="light dark">' in html
    assert "@media (prefers-color-scheme:dark)" in html
    assert "background:#fff" not in html


def test_status_is_a_chip_outside_the_title():
    """긴 status가 h1 안의 배지로 들어가 h1 글자 크기로 3줄 상자가 되고
    제목(task_id)을 눌렀다. status는 제목이 아니라 상태 표시다."""
    html = render(make_hard_view())
    h1 = html.split("<h1>")[1].split("</h1>")[0]
    assert "TASK-2026" in h1
    assert "LOOP_GUARD_EXCEEDED" not in h1


def test_missing_lead_time_is_named_not_placeholdered():
    """`?m`은 값처럼 보여서 버그로 읽힌다 — 측정이 없었다는 사실을 그대로 쓴다."""
    html = render(make_empty_view())
    assert "?m" not in html
    assert "미측정" in html


def test_empty_sections_say_so_instead_of_showing_a_headerless_table():
    """instances/loops가 0개일 때 헤더 행만 뜬 빈 표와 내용 없는 `<ul>`이 남아
    '데이터 없음'과 '렌더 실패'를 구분할 수 없었다."""
    html = render(make_empty_view())
    assert "<table" not in html
    assert "없습니다" in html


def test_template_version_tracks_the_redesign():
    """CSP·구조가 바뀌면 버전으로 추적한다 (DESIGN.md §15.5)."""
    assert 'data-template-version="poc-2"' in render(make_empty_view())


def test_status_tone_matches_tokens_not_substrings():
    """부분 문자열로 보면 `NOT_OK`가 `OK`를, `UNAPPROVED`가 `APPROVED`를 품어
    실패가 초록으로 칠해진다 — 글자는 실패라는데 칩은 성공색인 자리 (Codex 검토)."""
    from devcrew.report.renderer import _tone
    for bad in ("NOT_OK", "UNAPPROVED", "INCOMPLETE", "NOT_PASS", "BLOCKED"):
        assert _tone(bad, "OK", "NO") != "OK", bad
    for good in ("PASS", "DONE", "COMPLETE"):
        assert _tone(good, "OK", "NO") == "OK", good
    assert _tone("BYPASS", "OK", "NO") == ""          # 모르는 값은 중립


def test_muted_ink_meets_body_text_contrast():
    """보조 잉크는 빈 상태 문구·표 헤더 같은 **뜻을 지닌** 작은 글자에 쓰인다.
    라이트 3.18:1 / 다크 4.49:1이라 AA 4.5:1에 미달했다 (Codex 검토)."""
    html = render(make_empty_view())
    assert "--ink-3:#6e6c67" in html and "--ink-3:#93918a" in html
