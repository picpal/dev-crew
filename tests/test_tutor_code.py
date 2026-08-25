"""TUTOR_CODE — 실행 추적 파이프라인 (FakeAdapter).

핵심 불변조건: **코드는 모델이 아니라 하네스가 읽는다.** 모델은 범위만 지목하고,
범위 밖을 가리키는 스텝은 버려진다 — 지어낸 코드가 화면에 오르지 않게 하는 장치다.
"""
import dataclasses

import pytest

from devcrew.adapters.base import FakeAdapter
from devcrew.config import load as load_config
from devcrew.orchestrator import Orchestrator
from devcrew.schema import Provider
from devcrew.store.registry import SessionRegistry
from devcrew.store.trace import TraceStore


class Scripted(FakeAdapter):
    def __init__(self, structured):
        super().__init__(script=["ok"] * 20)
        self.queue = list(structured)

    def _next_structured(self, session_id, n):
        return self.queue.pop(0) if self.queue else super()._next_structured(session_id, n)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "app.py").write_text(
        "\n".join(f"def f{i}(): return {i}" for i in range(1, 31)) + "\n")
    return tmp_path


def _trace_out(steps, *, start=5, end=10, file="app.py"):
    return {"status": "PASS", "summary": "s", "title": "제목",
            "role_of_code": "이 코드의 역할", "file": file,
            "start_line": start, "end_line": end, "steps": steps}


def _step(line, reason="이유", vars=None):
    return {"line": line, "reason": reason,
            "vars": vars or [{"name": "x", "value": "1", "changed": True}]}


def make_orch(tmp_path, adapter):
    trace = TraceStore(tmp_path / "t.db")
    orch = Orchestrator(trace, SessionRegistry(tmp_path / "h.db"),
                        {Provider.CLAUDE_CODE: adapter, Provider.CODEX: adapter})
    return orch, trace


@pytest.mark.asyncio
async def test_trace_reads_the_code_from_disk_not_from_the_model(tmp_path, repo):
    """모델은 범위만 지목한다 — 왼쪽에 뜰 코드는 하네스가 파일에서 읽는다."""
    from devcrew.tutor_code import trace_code

    ad = Scripted([_trace_out([_step(5), _step(7), _step(9)])])
    orch, _ = make_orch(tmp_path, ad)
    r = await trace_code(orch, load_config(), exec_id="E", repo_path=str(repo),
                         focus_path="app.py", focus_symbol="f5", question="어떻게 도나?")
    assert [ln.number for ln in r.lines] == [5, 6, 7, 8, 9, 10]
    assert r.lines[0].text == "def f5(): return 5"
    assert len(r.steps) == 3 and r.dropped == 0


@pytest.mark.asyncio
async def test_steps_outside_the_shown_range_are_dropped(tmp_path, repo):
    """범위 밖 줄을 가리키는 스텝은 버린다 — 강조할 줄이 화면에 없다."""
    from devcrew.tutor_code import trace_code

    ad = Scripted([_trace_out([_step(5), _step(99), _step(0), _step(8)])])
    orch, _ = make_orch(tmp_path, ad)
    r = await trace_code(orch, load_config(), exec_id="E", repo_path=str(repo),
                         focus_path="app.py", focus_symbol=None, question="q")
    assert [s.line for s in r.steps] == [5, 8] and r.dropped == 2


@pytest.mark.asyncio
async def test_no_report_when_too_few_steps_survive(tmp_path, repo):
    """스텝이 하나뿐이면 애니메이션이 아니다 — 리포트를 만들지 않는다."""
    from devcrew.tutor_code import TraceUnavailable, trace_code

    ad = Scripted([_trace_out([_step(5), _step(99), _step(101)])])
    orch, _ = make_orch(tmp_path, ad)
    with pytest.raises(TraceUnavailable):
        await trace_code(orch, load_config(), exec_id="E", repo_path=str(repo),
                         focus_path="app.py", focus_symbol=None, question="q")


@pytest.mark.asyncio
async def test_path_escape_is_refused(tmp_path, repo):
    """`..`로 repo 밖을 지목하면 읽지 않는다 — 워커가 아니라 하네스가 여는 파일이라
    enforcement 경로 게이트가 걸리지 않는다. 여기서 막아야 한다."""
    from devcrew.tutor_code import TraceUnavailable, trace_code

    ad = Scripted([_trace_out([_step(5), _step(6)], file="../../etc/passwd")])
    orch, _ = make_orch(tmp_path, ad)
    with pytest.raises(TraceUnavailable):
        await trace_code(orch, load_config(), exec_id="E", repo_path=str(repo),
                         focus_path="x", focus_symbol=None, question="q")


@pytest.mark.asyncio
async def test_worker_is_reclaimed(tmp_path, repo):
    """1회용 세션이다 — 끝나면 프로세스와 registry 행을 둘 다 반납한다 (C13/C14)."""
    from devcrew.tutor_code import trace_code

    ad = Scripted([_trace_out([_step(5), _step(6)])])
    orch, _ = make_orch(tmp_path, ad)
    await trace_code(orch, load_config(), exec_id="E", repo_path=str(repo),
                     focus_path="app.py", focus_symbol=None, question="q")
    assert ad.archived, "워커를 반납하지 않았다"
