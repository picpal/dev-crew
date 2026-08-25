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
