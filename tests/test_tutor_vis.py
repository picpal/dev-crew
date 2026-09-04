"""TUTOR_VIS — 리포트용 다이어그램 (FakeAdapter).

핵심 불변조건 둘:
1. **사용자 repo에 쓰지 않는다** — 매번 임시 디렉토리를 만들어 cwd로 준다.
2. **모델이 낸 SVG를 그대로 믿지 않는다** — 스크립트·이벤트 핸들러·외부 참조를 뗀다.
"""
import dataclasses
from pathlib import Path

import pytest

from devcrew.adapters.base import FakeAdapter
from devcrew.config import load as load_config
from devcrew.orchestrator import Orchestrator
from devcrew.schema import Provider
from devcrew.store.registry import SessionRegistry
from devcrew.store.trace import TraceStore

SVG = ('<svg viewBox="0 0 100 50" xmlns="http://www.w3.org/2000/svg">'
       '<title>흐름</title><rect width="10" height="10"/></svg>')


class Drawing(FakeAdapter):
    """워커인 척하며 실제로 cwd에 파일을 떨군다 — 배선이 도는지 보려면 그래야 한다."""

    def __init__(self, html: str | None, *, name="diagram.html", status="PASS"):
        super().__init__(script=["ok"] * 10)
        self.html, self.name, self.status = html, name, status
        self.cwds: list[str] = []

    async def start_session(self, inst, initial_message, **kw):
        self.cwds.append(inst.worktree)
        if self.html is not None:
            Path(inst.worktree, self.name).write_text(self.html)
        return await super().start_session(inst, initial_message, **kw)

    def _next_structured(self, session_id, n):
        return {"status": self.status, "summary": "s", "file": self.name}


def make_orch(tmp_path, ad):
    trace = TraceStore(tmp_path / "t.db")
    return Orchestrator(trace, SessionRegistry(tmp_path / "h.db"),
                        {Provider.CLAUDE_CODE: ad, Provider.CODEX: ad})


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    (d / "a.py").write_text("x = 1\n")
    return d


@pytest.mark.asyncio
async def test_draws_in_a_scratch_dir_not_the_repo(tmp_path, repo):
    """작업 공간은 사용자 repo가 **아니다.** 끝나면 흔적도 남기지 않는다."""
    from devcrew.tutor_vis import draw

    ad = Drawing(f"<html><body>{SVG}</body></html>")
    svg = await draw(make_orch(tmp_path, ad), load_config(), exec_id="E",
                     spec="정지 흐름을 그려라")
    assert svg and svg.startswith("<svg")
    used = Path(ad.cwds[0]).resolve()
    assert repo.resolve() not in used.parents and used != repo.resolve()
    assert not used.exists(), "임시 디렉토리를 지우지 않았다"
    assert list(repo.iterdir()) == [repo / "a.py"], "사용자 repo가 더러워졌다"


@pytest.mark.asyncio
async def test_worker_is_reclaimed(tmp_path):
    from devcrew.tutor_vis import draw

    ad = Drawing(f"<html>{SVG}</html>")
    await draw(make_orch(tmp_path, ad), load_config(), exec_id="E", spec="s")
    assert ad.archived, "워커를 반납하지 않았다"


@pytest.mark.asyncio
async def test_script_and_handlers_and_external_refs_are_stripped(tmp_path):
    """모델이 낸 SVG는 비신뢰 입력이다. CSP가 backstop이지 1차 방어가 아니다."""
    from devcrew.tutor_vis import draw

    dirty = ('<svg xmlns="http://www.w3.org/2000/svg">'
             '<script>alert(1)</script>'
             '<rect onclick="steal()" onload="x()" width="5" height="5"/>'
             '<image href="https://evil.example/x.png"/>'
             '<a xlink:href="javascript:alert(1)">t</a>'
             '<foreignObject><div>주입</div></foreignObject>'
             '<circle r="3"/></svg>')
    ad = Drawing(f"<html>{dirty}</html>")
    svg = await draw(make_orch(tmp_path, ad), load_config(), exec_id="E", spec="s")
    assert svg
    for bad in ("<script", "onclick", "onload", "https://evil", "javascript:",
                "<foreignObject"):
        assert bad not in svg, f"{bad} 가 남았다"
    assert "<circle" in svg, "멀쩡한 도형까지 지웠다"


@pytest.mark.asyncio
async def test_missing_or_empty_output_returns_none(tmp_path):
    """그림이 없으면 없는 것이다 — 리포트는 그림 없이도 완결이어야 한다."""
    from devcrew.tutor_vis import draw

    cfg = load_config()
    assert await draw(make_orch(tmp_path, Drawing(None)), cfg, exec_id="E", spec="s") is None
    assert await draw(make_orch(tmp_path, Drawing("<html>svg 없음</html>")), cfg,
                      exec_id="E", spec="s") is None
    assert await draw(make_orch(tmp_path, Drawing(f"<html>{SVG}</html>", status="NOT_PASS")),
                      cfg, exec_id="E", spec="s") is None


@pytest.mark.asyncio
async def test_file_name_cannot_escape_the_scratch_dir(tmp_path):
    """모델이 낸 `file`은 경로다 — `../`로 밖을 가리키면 읽지 않는다."""
    from devcrew.tutor_vis import draw

    (tmp_path / "secret.html").write_text(f"<html>{SVG}</html>")
    ad = Drawing(f"<html>{SVG}</html>", name="../../secret.html")
    assert await draw(make_orch(tmp_path, ad), load_config(), exec_id="E", spec="s") is None


def test_palette_maps_to_report_tokens_in_both_themes():
    """vision 기본 팔레트를 **우리 토큰**으로 바꾼다 (사용자 2026-08-25).

    리포트는 테마 반응형이다(`prefers-color-scheme`). 그래서 vision의 다크 템플릿을
    고르면 다크는 맞고 **라이트가 반대로 어긋난다.** 색을 토큰에 매핑하면 그림이 페이지
    테마를 그대로 따라가 두 모드가 다 맞는다.

    같은 hex라도 **속성에 따라 뜻이 다르다** — `fill="#2d3142"`는 글자고
    `stroke="#2d3142"`는 테두리다. 테두리까지 `--ink-1`로 보내면 다크에서 순백 테두리가
    돼 그림이 소리친다.
    """
    from devcrew.tutor_vis import theme_svg

    out = theme_svg('<rect fill="#ffffff" stroke="#2d3142"/>'
                    '<text fill="#2d3142">글</text>'
                    '<text fill="#4f5d75">보조</text>'
                    '<line stroke="#4f5d75"/>'
                    '<rect width="100%" fill="#f5f5f5"/>'
                    '<line stroke="#0f766e"/>')
    assert 'fill="var(--surface, #ffffff)"' in out
    assert 'stroke="var(--ink-2, #2d3142)"' in out
    assert 'fill="var(--ink-1, #2d3142)"' in out
    assert 'fill="var(--ink-2, #4f5d75)"' in out
    assert 'stroke="var(--ink-3, #4f5d75)"' in out
    assert 'fill="var(--surface-2, #f5f5f5)"' in out
    assert 'stroke="var(--good, #0f766e)"' in out


def test_fallback_keeps_the_diagram_readable_standalone():
    """토큰이 없는 곳(파일로 따로 열기 등)에서도 원래 색으로 그려져야 한다."""
    from devcrew.tutor_vis import theme_svg

    out = theme_svg('<text fill="#2d3142">x</text>')
    assert "#2d3142" in out, "폴백 색이 사라졌다"


def test_unknown_colors_are_left_alone():
    """기본 팔레트 밖의 색은 건드리지 않는다 — 뜻을 모르는 색을 옮기면 그림이 망가진다."""
    from devcrew.tutor_vis import theme_svg

    out = theme_svg('<rect fill="#c22f2f"/><circle fill="url(#grad)"/>')
    assert 'fill="#c22f2f"' in out and 'fill="url(#grad)"' in out


def test_accent_tint_becomes_a_token_mix():
    """강조 색의 옅은 채움도 따라가야 한다 — 안 그러면 다크에서 그 면만 뜬다."""
    from devcrew.tutor_vis import theme_svg

    out = theme_svg('<rect fill="rgba(15,118,110,0.08)"/>')
    assert "color-mix(in srgb, var(--good, #0f766e) 8%, transparent)" in out


@pytest.mark.asyncio
async def test_draw_applies_the_theme(tmp_path):
    """배선 — `draw()`가 살균만 하고 테마 매핑을 빠뜨리면 안 된다."""
    from devcrew.tutor_vis import draw

    svg = ('<svg xmlns="http://www.w3.org/2000/svg">'
           '<text fill="#2d3142">글</text></svg>')
    ad = Drawing(f"<html>{svg}</html>")
    out = await draw(make_orch(tmp_path, ad), load_config(), exec_id="E", spec="s")
    assert out and "var(--ink-1" in out


def test_real_skill_output_survives_extraction():
    """`vision` 스킬이 실제로 낸 HTML(2026-09-04 실측)에서 SVG가 온전히 나오는지.

    상한을 올리기 전에 확인할 것은 "시간만 주면 쓸 수 있는 결과가 나오는가"였다.
    파이프라인이 실물 산출물을 못 다루면 상한을 올려도 소용이 없다.
    """
    from devcrew.tutor_vis import MAX_SVG, extract_svg

    html = ('<!doctype html><html><head><style>.n{fill:#123}</style></head><body>'
            '<svg viewBox="0 0 504 276" xmlns="http://www.w3.org/2000/svg" '
            'role="img" aria-labelledby="t d"><title id="t">리밸런싱</title>'
            '<g class="n"><rect x="1" y="2" width="3" height="4"/>'
            '<text x="5" y="6">이탈 감지</text></g></svg>'
            '<script>console.log(1)</script></body></html>')
    svg = extract_svg(html)
    assert svg and svg.startswith("<svg")
    assert "이탈 감지" in svg and "console.log" not in svg
    assert len(svg) < MAX_SVG


def test_timeout_has_headroom_over_the_measured_run():
    """상한은 실측 뒤에 정한다 (lessons C3). 그리기 실측은 408초였다."""
    from devcrew.tutor_vis import TURN_TIMEOUT

    assert TURN_TIMEOUT >= 408 * 2
