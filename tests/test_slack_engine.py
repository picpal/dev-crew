"""slack_engine — MentionHandler 순수 로직 검증 (Slack 네트워크·LLM 없음)."""
import asyncio
from dataclasses import dataclass, field

import pytest

from devcrew.slack_engine import MentionHandler, format_result


@dataclass
class FakeResult:
    status: str = "COMPLETED"
    node_history: list = field(default_factory=lambda: [
        {"node_id": "develop", "transition": "PASS", "iteration": 0},
        {"node_id": "review", "transition": "PASS", "iteration": 0}])
    decisions: int = 1
    total_tokens: int = 12345


class FakeRunner:
    def __init__(self, *, error: Exception | None = None, timeout: bool = False,
                 warnings: list[str] | None = None):
        self.error, self.timeout_flag = error, timeout
        self.warnings = list(warnings or [])
        self.stopped: list[str] = []
        self.timeout = 600.0
        self.busy = False
        self.calls: list[str] = []
        self.thread_keys: list = []

    def request_stop(self, execution_id: str) -> None:
        self.stopped.append(execution_id)

    async def run(self, task: str, *, on_progress=None, on_warning=None, thread_key=None):
        self.calls.append(task)
        self.thread_keys.append(thread_key)
        for w in self.warnings:                       # 실행 중 예산 경보 재현
            await on_warning("SLACK-1", w)
        if self.timeout_flag:
            raise asyncio.TimeoutError
        if self.error:
            raise self.error
        return "SLACK-1", FakeResult(), "/tmp/repo"


class SaySpy:
    def __init__(self):
        self.messages: list[dict] = []

    async def __call__(self, *, text: str, thread_ts=None, blocks=None):
        self.messages.append({"text": text, "thread_ts": thread_ts, "blocks": blocks})


def body(text: str, event_id: str = "Ev1", ts: str = "111.222") -> dict:
    return {"event_id": event_id, "event": {"text": text, "ts": ts}}


@pytest.mark.asyncio
async def test_mention_runs_task_and_replies_in_thread():
    runner, say = FakeRunner(), SaySpy()
    await MentionHandler(runner)(body("<@U123> calc.py에 mul 추가"), say)
    assert runner.calls == ["calc.py에 mul 추가"]           # mention 제거된 task
    assert len(say.messages) == 1                            # 최종 결과만 (상태는 인디케이터)
    assert "✅ *SLACK-1 COMPLETED*" in say.messages[0]["text"]
    assert say.messages[0]["thread_ts"] == "111.222"
    # 스레드 단위 세션 이월 키로 thread_ts가 그대로 넘어간다
    assert runner.thread_keys == ["111.222"]


@pytest.mark.asyncio
async def test_duplicate_event_id_is_ignored():
    runner, say = FakeRunner(), SaySpy()
    h = MentionHandler(runner)
    await h(body("<@U123> t", event_id="EvX"), say)
    await h(body("<@U123> t", event_id="EvX"), say)           # Slack 재전송
    assert runner.calls == ["t"]                              # 1회만 실행
    assert len(say.messages) == 1


@pytest.mark.asyncio
async def test_empty_task_gets_usage_hint_without_running():
    runner, say = FakeRunner(), SaySpy()
    await MentionHandler(runner)(body("<@U123>   "), say)
    assert runner.calls == []
    assert "⚠️" in say.messages[0]["text"]


@pytest.mark.asyncio
async def test_engine_exception_reported_to_thread():
    runner, say = FakeRunner(error=RuntimeError("boom")), SaySpy()
    await MentionHandler(runner)(body("<@U123> t"), say)
    assert "💥 실행 실패: RuntimeError: boom" in say.messages[-1]["text"]


@pytest.mark.asyncio
async def test_timeout_reported_to_thread():
    runner, say = FakeRunner(timeout=True), SaySpy()
    await MentionHandler(runner)(body("<@U123> t"), say)
    assert "⏰ 타임아웃" in say.messages[-1]["text"]


def test_format_result_needs_human_icon():
    r = FakeResult(status="NEEDS_HUMAN")
    text = format_result("SLACK-2", r, "/tmp/repo")
    assert text.startswith("🙋 *SLACK-2 NEEDS_HUMAN*")
    assert "`develop:PASS → review:PASS`" in text
    assert "토큰 12,345" in text


def test_progress_text_shows_stage_and_path():
    from devcrew.slack_engine import progress_text
    events = [
        {"event_type": "ModelRoutingEvent", "payload": {"role": "DEVELOPER"}},
        {"event_type": "NodeTransitionEvent", "payload": {"node_id": "develop", "transition": "PASS"}},
        {"event_type": "ModelRoutingEvent", "payload": {"role": "REVIEWER"}},
        {"event_type": "DecisionEvent", "payload": {}},
    ]
    t = progress_text(events, 32.7)
    assert "REVIEWER 실행 중 (32s)" in t
    assert "develop:PASS" in t and "결정 1회" in t
    assert "\n" not in t                                     # status는 한 줄


def test_format_result_shows_termination_reason_and_full_path():
    """예산 초과로 끝난 실행은 사유가 메시지에 그대로 보여야 하고, 경로에는
    crew leader 결정 홉까지 포함된 전체 경로가 찍혀야 한다 (사용자 요청 2026-08-20)."""
    @dataclass
    class R:
        status: str = "NEEDS_HUMAN"
        node_history: list = field(default_factory=list)
        decisions: int = 2
        total_tokens: int = 587872
        reason: str = "실행 전체 토큰 예산 초과 (587,872/300,000)"
        path: list = field(default_factory=lambda: [
            "leader:CLASSIFY→PROCEED", "explore:PASS", "develop:PASS",
            "review:NOT_PASS↺develop", "develop:PASS", "review:PASS",
            "leader:LOOP_GUARD_EXCEEDED[1]", "leader:LOOP_GUARD_EXCEEDED→ASK_USER"])
        role_tokens: dict = field(default_factory=lambda: {
            "DEVELOPER": 300000, "ORCHESTRATOR": 200000, "REVIEWER": 87872})

    text = format_result("SLACK-2", R(), "/tmp/repo")
    assert "*사유* 실행 전체 토큰 예산 초과 (587,872/300,000)" in text
    assert "leader:CLASSIFY→PROCEED → explore:PASS" in text
    assert "review:NOT_PASS↺develop" in text
    assert "leader:LOOP_GUARD_EXCEEDED→ASK_USER" in text
    # 에이전트별 토큰은 많이 쓴 순으로
    assert "DEVELOPER 300,000 · ORCHESTRATOR 200,000 · REVIEWER 87,872" in text


def test_format_result_without_new_fields_still_renders():
    """path/reason이 없는 결과(구 형식)도 node_history 폴백으로 렌더된다."""
    text = format_result("SLACK-9", FakeResult(), "/tmp/repo")
    assert "`develop:PASS → review:PASS`" in text
    assert "*사유*" not in text


def test_format_result_renders_budget_warnings_separately_from_reason():
    """role 예산 초과는 경보 줄로만 나오고 종료 사유가 되지 않는다."""
    @dataclass
    class R:
        status: str = "COMPLETED"
        node_history: list = field(default_factory=list)
        decisions: int = 0
        total_tokens: int = 700000
        reason: str = "모든 노드 통과"
        path: list = field(default_factory=lambda: ["develop:PASS", "review:PASS"])
        role_tokens: dict = field(default_factory=lambda: {"DEVELOPER": 700000})
        warnings: list = field(default_factory=lambda: ["DEVELOPER 예산 초과 (700,000/600,000)"])

    text = format_result("SLACK-3", R(), "/tmp/repo")
    assert text.startswith("✅ *SLACK-3 COMPLETED*")
    assert "⚠️ DEVELOPER 예산 초과 (700,000/600,000)" in text
    assert "*사유* 모든 노드 통과" in text


@pytest.mark.asyncio
async def test_budget_warning_posts_stop_button_and_run_continues():
    """예산 경보가 뜨면 중지 버튼이 달린 메시지를 스레드에 올리되, 실행은 계속돼
    최종 결과가 그대로 회신된다 (경보 ≠ 종료)."""
    from devcrew.slack_engine import STOP_ACTION
    runner = FakeRunner(warnings=["DEVELOPER 예산 초과 (700,000/600,000)"])
    say = SaySpy()
    await MentionHandler(runner)(body("<@U123> t"), say)

    warn_msg, final_msg = say.messages[0], say.messages[-1]
    assert "예산 경보" in warn_msg["text"]
    button = warn_msg["blocks"][-1]["elements"][0]
    assert button["action_id"] == STOP_ACTION
    assert button["value"] == "SLACK-1"
    assert button["style"] == "danger"
    assert warn_msg["thread_ts"] == "111.222"
    assert "✅ *SLACK-1 COMPLETED*" in final_msg["text"]      # 실행은 완주했다


def test_stop_blocks_and_stopped_blocks_shape():
    from devcrew.slack_engine import STOP_ACTION, stop_blocks, stopped_blocks
    blocks = stop_blocks("SLACK-7", "DEVELOPER 예산 초과 (1/0)")
    assert blocks[0]["type"] == "section" and "SLACK-7" in blocks[0]["text"]["text"]
    assert blocks[-1]["elements"][0]["action_id"] == STOP_ACTION
    # 중지 요청 후에는 버튼이 사라진다 (중복 클릭 방지)
    after = stopped_blocks("SLACK-7")
    assert all(b["type"] != "actions" for b in after)
    assert "중지 요청됨" in after[0]["text"]["text"]


def test_format_result_stopped_icon():
    @dataclass
    class R:
        status: str = "STOPPED"
        node_history: list = field(default_factory=list)
        decisions: int = 0
        total_tokens: int = 1000
        reason: str = "사용자가 실행을 중지했다"
        path: list = field(default_factory=lambda: ["develop:PASS", "STOPPED"])
        role_tokens: dict = field(default_factory=dict)
        warnings: list = field(default_factory=list)

    text = format_result("SLACK-4", R(), "/tmp/repo")
    assert text.startswith("🛑 *SLACK-4 STOPPED*")
    assert "*사유* 사용자가 실행을 중지했다" in text


# ---------------------------------------------------------------------------
# EngineRunner 배선 회귀 고정 — 스레드 이월/중지가 실제로 엔진까지 전달되는가
# (이 배선이 조용히 빠져도 엔진 단위 테스트는 전부 통과한다 — 여기서 잡는다)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_runner_reuses_workspace_and_carry_per_thread(tmp_path, monkeypatch):
    import devcrew.decision as decision_mod
    import devcrew.engine as engine_mod
    from devcrew.slack_engine import EngineRunner

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("DEVCREW_TARGET_REPO", str(repo))

    seen: list[dict] = []

    class RecordingEngine:
        def __init__(self, orch, cfg, **kw):
            self.kw = kw

        async def run(self, *, execution_id, task, worktree=None, carry=None):
            seen.append({"execution_id": execution_id, "worktree": worktree,
                         "carry": carry, "stop_check": self.kw.get("stop_check")})
            carry["develop"] = {"inst": object(), "session_id": f"s-{execution_id}",
                                "tier": "DEFAULT"}
            return engine_mod.ExecutionResult("COMPLETED", [], 0, 0)

    monkeypatch.setattr(engine_mod, "WorkflowEngine", RecordingEngine)
    monkeypatch.setattr(decision_mod, "make_llm_decide", lambda *a, **k: None)

    runner = EngineRunner(runtime_dir=tmp_path / "rt")
    await runner.run("첫 요청", thread_key="T1")
    await runner.run("이어서", thread_key="T1")
    await runner.run("다른 스레드", thread_key="T2")

    # 같은 스레드: 작업 공간과 carry(노드 세션)를 그대로 이어받는다
    assert seen[0]["worktree"] == seen[1]["worktree"]
    assert seen[1]["carry"] is seen[0]["carry"]
    assert "develop" in seen[1]["carry"]
    # 다른 스레드: 새 carry (앞 스레드 세션을 물려받지 않는다)
    assert seen[2]["carry"] is not seen[0]["carry"]
    assert seen[2]["carry"] == {"develop": seen[2]["carry"]["develop"]}
    # execution_id는 요청마다 새로 발급된다
    assert len({s["execution_id"] for s in seen}) == 3


@pytest.mark.asyncio
async def test_runner_stop_request_reaches_engine(tmp_path, monkeypatch):
    import devcrew.decision as decision_mod
    import devcrew.engine as engine_mod
    from devcrew.slack_engine import EngineRunner

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("DEVCREW_TARGET_REPO", str(repo))
    box: dict = {}

    class RecordingEngine:
        def __init__(self, orch, cfg, **kw):
            self.kw = kw

        async def run(self, *, execution_id, task, worktree=None, carry=None):
            box["stop_check"] = self.kw["stop_check"]
            box["before"] = self.kw["stop_check"]()
            runner.request_stop(execution_id)                # 사용자가 버튼 클릭
            box["after"] = self.kw["stop_check"]()
            return engine_mod.ExecutionResult("STOPPED", [], 0, 0)

    monkeypatch.setattr(engine_mod, "WorkflowEngine", RecordingEngine)
    monkeypatch.setattr(decision_mod, "make_llm_decide", lambda *a, **k: None)

    runner = EngineRunner(runtime_dir=tmp_path / "rt")
    await runner.run("t", thread_key="T1")
    assert box["before"] is False and box["after"] is True
    assert runner._stop_requests == set()                    # 실행 종료 후 정리된다


def test_format_result_uses_leader_report_as_body():
    """본문은 leader가 쓴 보고문이고, 하네스 수치는 각주로 내려간다."""
    @dataclass
    class R:
        status: str = "COMPLETED"
        node_history: list = field(default_factory=list)
        decisions: int = 2
        total_tokens: int = 84120
        reason: str = "모든 노드 통과"
        path: list = field(default_factory=lambda: ["develop:PASS", "review:PASS"])
        role_tokens: dict = field(default_factory=lambda: {"DEVELOPER": 50000})
        warnings: list = field(default_factory=list)
        report: str = ("`_tokens()` 회귀 테스트를 추가했습니다.\n\n"
                       "*확인된 것*\n• 테스트 205건 통과")

    text = format_result("SLACK-5", R(), "dev-crew · wt/slack-5")
    assert text.startswith("✅ *SLACK-5*\n\n")          # 상태 문자열 중복 없이 보고문이 본문
    assert "회귀 테스트를 추가했습니다" in text
    assert "*확인된 것*" in text
    # 수치는 본문 뒤 각주로
    assert text.index("회귀 테스트") < text.index("*경로*")
    assert "*작업 공간* dev-crew · wt/slack-5" in text


def test_leader_report_cannot_broadcast_to_channel():
    """leader 보고문은 워커의 신뢰 불가 보고를 재료로 쓴다 — 채널 전체 알림을
    유발하는 토큰이 그대로 게시되면 안 된다."""
    from devcrew.slack_engine import sanitize_slack

    @dataclass
    class R:
        status: str = "COMPLETED"
        node_history: list = field(default_factory=list)
        decisions: int = 0
        total_tokens: int = 1
        reason: str = ""
        path: list = field(default_factory=list)
        role_tokens: dict = field(default_factory=dict)
        warnings: list = field(default_factory=list)
        report: str = "완료 <!channel> <!here|여기> <!everyone>"

    text = format_result("SLACK-6", R(), "/tmp/r")
    assert "<!channel>" not in text and "<!here" not in text and "<!everyone>" not in text
    assert "@channel" in text and "@here" in text          # 내용은 남되 알림은 안 간다
    assert sanitize_slack("정상 `코드` *굵게*") == "정상 `코드` *굵게*"


@pytest.mark.asyncio
async def test_runner_passes_request_branch_to_worktree(tmp_path, monkeypatch):
    """`repo@브랜치:` 접두가 실제 worktree 생성까지 도달해야 한다.

    회귀 고정: 파싱만 테스트하면 slack_engine이 브랜치를 버리는 2-tuple 래퍼를
    써도 통과한다 (실제로 그렇게 죽어 있었다)."""
    import devcrew.decision as decision_mod
    import devcrew.engine as engine_mod
    import devcrew.slack_engine as se

    repo = tmp_path / "repo"
    repo.mkdir()
    created: list[tuple] = []

    class FakeWT:
        def __init__(self, root):
            self.root = root

        def create(self, name, base_ref="HEAD"):
            created.append((name, base_ref))
            d = tmp_path / "wt" / name
            d.mkdir(parents=True)
            return d

    class NoopEngine:
        def __init__(self, orch, cfg, **kw):
            pass

        async def run(self, *, execution_id, task, worktree=None, carry=None):
            return engine_mod.ExecutionResult("COMPLETED", [], 0, 0)

    monkeypatch.setattr(engine_mod, "WorkflowEngine", NoopEngine)
    monkeypatch.setattr(decision_mod, "make_llm_decide", lambda *a, **k: None)
    monkeypatch.setattr(se, "WorktreeManager", FakeWT)

    runner = se.EngineRunner(runtime_dir=tmp_path / "rt")
    runner.repos = {"demo": repo}
    runner.repo_bases = {"demo": "main"}

    await runner.run("demo: 기본 base", thread_key="A")
    await runner.run("demo@feat/x: 요청 base", thread_key="B")

    assert created[0][1] == "main"        # repos.yaml 고정 base
    assert created[1][1] == "feat/x"      # 요청 접두가 덮어쓴다


@pytest.mark.asyncio
async def test_runner_breaks_carry_when_branch_changes_in_thread(tmp_path, monkeypatch):
    """같은 스레드에서 base 브랜치를 바꾸면 세션 이월을 끊는다 (다른 커밋의 트리)."""
    import devcrew.decision as decision_mod
    import devcrew.engine as engine_mod
    import devcrew.slack_engine as se

    repo = tmp_path / "repo"
    repo.mkdir()
    carries: list = []

    class FakeWT:
        def __init__(self, root):
            pass

        def create(self, name, base_ref="HEAD"):
            d = tmp_path / "wt" / name
            d.mkdir(parents=True)
            return d

    class NoopEngine:
        def __init__(self, orch, cfg, **kw):
            pass

        async def run(self, *, execution_id, task, worktree=None, carry=None):
            carries.append(carry)
            carry["develop"] = {"inst": object(), "session_id": "s", "tier": "DEFAULT"}
            return engine_mod.ExecutionResult("COMPLETED", [], 0, 0)

    monkeypatch.setattr(engine_mod, "WorkflowEngine", NoopEngine)
    monkeypatch.setattr(decision_mod, "make_llm_decide", lambda *a, **k: None)
    monkeypatch.setattr(se, "WorktreeManager", FakeWT)

    runner = se.EngineRunner(runtime_dir=tmp_path / "rt")
    runner.repos = {"demo": repo}
    runner.repo_bases = {"demo": "main"}

    await runner.run("demo@feat/a: 1", thread_key="T")
    await runner.run("demo@feat/a: 2", thread_key="T")     # 같은 base → 이월
    await runner.run("demo@feat/b: 3", thread_key="T")     # base 변경 → 이월 끊김
    assert carries[1] is carries[0]
    assert carries[2] is not carries[0]


@pytest.mark.asyncio
async def test_crew_dispatch_pins_rehandoff_to_first_thread():
    """재인계는 첫 핸드오프 스레드로 되돌아가야 crew leader 컨텍스트가 이어진다."""
    from devcrew.slack_engine import make_crew_dispatch

    posted, events = [], []
    ts_seq = iter(["200.1", "200.2"])

    async def post_handoff(channel, text, thread_ts):
        posted.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return next(ts_seq)

    async def post_crew(channel, text, thread_ts, **kw):
        posted.append({"crew": text, "thread_ts": thread_ts})

    async def handler(body, say):
        events.append(body["event"])
        await say(text="결과")

    dispatch = make_crew_dispatch(post_handoff, post_crew, handler)
    await dispatch("작업1", "C1", "100.1", "brief1")
    await dispatch("작업2", "C1", "100.1", "brief2")

    assert posted[0]["thread_ts"] is None                  # 첫 인계는 채널 최상위
    assert posted[2]["thread_ts"] == "200.1"               # 재인계는 그 스레드 안으로
    assert "추가 인계" in posted[2]["text"]
    # crew thread_key(= event.thread_ts)가 두 번 다 같아야 세션 이월이 산다
    assert [e["thread_ts"] for e in events] == ["200.1", "200.1"]
    assert all(p["thread_ts"] == "200.1" for p in posted if "crew" in p)


@pytest.mark.asyncio
async def test_crew_dispatch_recovers_pinned_thread_from_trace(tmp_path):
    """재시작으로 in-memory roots가 비어도 같은 인터뷰는 같은 crew 스레드로 간다."""
    from devcrew.slack_engine import make_crew_dispatch
    from devcrew.store.trace import TraceStore

    trace = TraceStore(tmp_path / "t.db")
    events = []
    ts_seq = iter(["300.1", "300.2"])

    async def post_handoff(channel, text, thread_ts):
        events.append(("post", thread_ts))
        return next(ts_seq)

    async def post_crew(channel, text, thread_ts, **kw):
        pass

    async def handler(body, say):
        events.append(("run", body["event"]["thread_ts"]))

    await make_crew_dispatch(post_handoff, post_crew, handler, trace=trace)(
        "작업1", "C1", "100.1", "brief1")
    # 새 프로세스: roots 캐시가 비어 있다
    await make_crew_dispatch(post_handoff, post_crew, handler, trace=trace)(
        "작업2", "C1", "100.1", "brief2")

    assert events == [("post", None), ("run", "300.1"),
                      ("post", "300.1"), ("run", "300.1")]


@pytest.mark.asyncio
async def test_crew_dispatch_drops_pin_after_interview_closed(tmp_path):
    """종료 후 같은 스레드에서 시작된 새 인터뷰가 이전 crew 스레드를 물려받으면 안 된다."""
    from devcrew.slack_engine import make_crew_dispatch
    from devcrew.store.trace import TraceStore

    trace = TraceStore(tmp_path / "t.db")
    posts, ts_seq = [], iter(["400.1", "400.2"])

    async def post_handoff(channel, text, thread_ts):
        posts.append(thread_ts)
        return next(ts_seq)

    async def post_crew(channel, text, thread_ts, **kw):
        pass

    async def handler(body, say):
        pass

    dispatch = make_crew_dispatch(post_handoff, post_crew, handler, trace=trace)
    await dispatch("작업1", "C1", "100.1", "brief1")
    trace.append("BrainClosedEvent", task_id="BRAIN-100.1",
                 execution_id="BRAIN-100.1", payload={"topic": "끝"})
    await dispatch("작업2", "C1", "100.1", "brief2")
    assert posts == [None, None]                  # 새 인터뷰는 새 crew 스레드로


@pytest.mark.asyncio
async def test_slack_posters_route_and_pin_threads():
    """_amain의 게시 어댑터 — 클로저 안에 두면 테스트가 못 닿는 자리였다."""
    from devcrew.slack_engine import slack_posters

    class Client:
        def __init__(self, name):
            self.name, self.calls = name, []

        async def chat_postMessage(self, **kw):
            self.calls.append(kw)
            return {"ts": "500.1"}

    brain, crew = Client("brain"), Client("crew")
    post_handoff, post_crew = slack_posters(brain, crew)

    assert await post_handoff("C1", "brief", None) == "500.1"
    assert brain.calls[0]["thread_ts"] is None and crew.calls == []

    await post_crew("C1", "결과", "500.1", blocks=[{"x": 1}])
    assert crew.calls[0]["thread_ts"] == "500.1"          # crew 회신은 고정 root로
    assert crew.calls[0]["blocks"] == [{"x": 1}]
    assert len(brain.calls) == 1                          # 서로의 봇 토큰을 섞지 않는다


class ClearSpy:
    """clear_thread/busy_for만 필요한 최소 러너 대역."""

    def __init__(self, has_state=True, busy=False):
        self.state = {"repo_name": "demo", "nodes": {}, "leader": {}} if has_state else None
        self.busy_flag, self.cleared = busy, []
        self.timeout = 600.0

    def busy_for(self, thread_key):
        return self.busy_flag

    async def clear_thread(self, thread_key):
        self.cleared.append(thread_key)
        return self.state


@pytest.mark.asyncio
async def test_crew_slash_clear_drops_thread_context():
    from devcrew.slack_engine import MentionHandler
    runner = ClearSpy()
    say = SaySpy()
    await MentionHandler(runner)({"event": {"text": "<@U1> /clear", "ts": "T1",
                                            "channel": "C1"}}, say)
    assert runner.cleared == ["T1"]
    assert "컨텍스트를 비웠습니다" in say.messages[-1]["text"]


@pytest.mark.asyncio
async def test_crew_clear_refused_while_running():
    from devcrew.slack_engine import MentionHandler
    runner = ClearSpy(busy=True)
    say = SaySpy()
    await MentionHandler(runner)({"event": {"text": "<@U1> /clear", "ts": "T1",
                                            "channel": "C1"}}, say)
    assert runner.cleared == []                     # 진행 중 실행을 끊지 않는다
    assert "실행 중지" in say.messages[-1]["text"]


@pytest.mark.asyncio
async def test_clear_thread_archives_carried_sessions(tmp_path, monkeypatch):
    """이월 세션을 안 닫고 상태만 지우면 SDK 프로세스가 그대로 남는다."""
    import devcrew.slack_engine as se
    from devcrew.schema import AgentInstance, EffortLevel, Provider, Role

    runner = se.EngineRunner(runtime_dir=tmp_path / "rt")
    archived = []

    class Adapter:
        async def archive(self, sid):
            archived.append(sid)
            return "ARCHIVED"

    runner.orch.adapters[Provider.CLAUDE_CODE] = Adapter()
    inst = AgentInstance(
        instance_id="DEV-1", role=Role.DEVELOPER, provider=Provider.CLAUDE_CODE,
        adapter="fake", model="m", effort_level=EffortLevel.HIGH, reasoning_level=None,
        routing_policy_version="v1", routing_reason="test", session_id=None,
        execution_id="SLACK-1", workflow_id="wf", node_id="develop", task_scope="s",
        worktree=None)
    runner._threads["T9"] = {
        "repo_name": "demo", "workspace": "/tmp/x", "where": "demo", "base": "main",
        "nodes": {"develop": {"inst": inst, "session_id": "sid-node"}},
        "leader": {"inst": inst, "sid": "sid-leader"}}

    assert await runner.clear_thread("T9") is not None
    assert sorted(archived) == ["sid-leader", "sid-node"]
    assert "T9" not in runner._threads
    assert runner.trace.events(event_type="ThreadContextClearedEvent")
    assert await runner.clear_thread("T9") is None      # 두 번째는 비울 게 없다


def test_format_result_puts_context_badge_on_top():
    import dataclasses
    from devcrew.engine import ExecutionResult

    r = ExecutionResult("COMPLETED", [], 1, 10, report="다 됐습니다",
                        context_used=620_000, context_window=1_000_000)
    out = format_result("SLACK-9", r, "/tmp/wt")
    assert out.splitlines()[0] == "[context usage : 62%]"
    assert "다 됐습니다" in out

    quiet = dataclasses.replace(r, context_used=100_000)
    assert not format_result("SLACK-9", quiet, "/tmp/wt").startswith("[context")


# ── @tutor 배선 (lessons.md C1: 클로저 안 배선은 테스트가 닿지 않는다) ──────
def test_tutor_action_routes_button_value_to_handler():
    """버튼 클릭 → 핸들러로 (thread, value, user)가 흐르는지. 배선이 유실되면 잡힌다."""
    import asyncio

    from devcrew.slack_engine import make_tutor_action

    seen = {}

    class H:
        async def on_answer(self, *, thread_ts, value, say, strip=None,
                            channel="", user=""):
            seen.update(thread_ts=thread_ts, value=value, channel=channel, user=user)
            await strip()

    updated = []

    class Client:
        async def chat_update(self, **kw):
            updated.append(kw)

        async def chat_postMessage(self, **kw):
            return kw

    handler = make_tutor_action(H(), Client())
    body = {"channel": {"id": "C9"}, "user": {"id": "U7"},
            "message": {"ts": "50.1", "thread_ts": "40.1", "text": "문항 3?"},
            "actions": [{"value": "2:1"}]}
    asyncio.run(handler(body))
    assert seen == {"thread_ts": "40.1", "value": "2:1", "channel": "C9", "user": "U7"}
    assert updated and "✅ 선택: B" in updated[0]["text"]     # 고른 보기가 남는다


def test_tutor_action_marks_the_chosen_option_letter():
    import asyncio

    from devcrew.slack_engine import make_tutor_action

    class H:
        async def on_answer(self, *, thread_ts, value, say, strip=None,
                            channel="", user=""):
            await strip()

    updated = []

    class Client:
        async def chat_update(self, **kw):
            updated.append(kw)

    asyncio.run(make_tutor_action(H(), Client())(
        {"channel": {"id": "C1"}, "user": {"id": "U1"},
         "message": {"ts": "1.1", "thread_ts": "1.0", "text": "q"},
         "actions": [{"value": "0:3"}]}))
    assert "✅ 선택: D" in updated[0]["text"]


def test_tutor_action_labels_regrade_without_a_fake_option_letter():
    """`✅ 선택: ?`는 사용자를 헷갈리게 한다 — 재채점은 선택이 아니다."""
    import asyncio

    from devcrew.slack_engine import make_tutor_action
    from devcrew.slack_tutor import REGRADE_VALUE

    class H:
        async def on_answer(self, *, thread_ts, value, say, strip=None,
                            channel="", user=""):
            await strip()

    updated = []

    class Client:
        async def chat_update(self, **kw):
            updated.append(kw)

    asyncio.run(make_tutor_action(H(), Client())(
        {"channel": {"id": "C1"}, "user": {"id": "U1"},
         "message": {"ts": "1.1", "thread_ts": "1.0", "text": "채점 실패"},
         "actions": [{"value": REGRADE_VALUE}]}))
    assert "🔁 다시 채점 중" in updated[0]["text"] and "선택: ?" not in updated[0]["text"]


# ── .env 로딩 ───────────────────────────────────────────────────────────────
def test_dotenv_fills_missing_vars_without_overriding_the_shell(tmp_path, monkeypatch):
    """이미 export한 값이 파일에 밀리면 안 된다 — 셸이 항상 이긴다."""
    from devcrew.slack_engine import load_env
    (tmp_path / ".env").write_text(
        "# 주석\nTUTOR_BOT_TOKEN=xoxb-from-file\nSLACK_BOT_TOKEN=xoxb-from-file\n")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-shell")
    monkeypatch.delenv("TUTOR_BOT_TOKEN", raising=False)
    load_env(tmp_path / ".env")
    import os
    assert os.environ["TUTOR_BOT_TOKEN"] == "xoxb-from-file"
    assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-from-shell"


def test_missing_dotenv_is_not_an_error(tmp_path):
    """.env 없이 export만 쓰던 기존 방식이 그대로 돌아야 한다."""
    from devcrew.slack_engine import load_env
    assert load_env(tmp_path / "nope.env") is False


def test_env_is_found_in_the_main_repo_when_a_worktree_has_none(tmp_path):
    """워크트리는 버려지는 공간이다 — 비밀은 본체 저장소에 두고 워크트리가 그걸 읽는다.

    2026-08-22: 워크트리 정리 도구가 .worktrees/*를 치우면서 그 안의 .env가 함께
    사라졌다. 토큰을 워크트리에 둔 것이 잘못이었다."""
    import os

    from devcrew.slack_engine import env_candidates
    main = tmp_path / "repo"
    (main / ".git" / "worktrees" / "wt1").mkdir(parents=True)
    (main / ".env").write_text("X=1\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt1\n")
    assert main / ".env" in env_candidates(wt)


def test_worktree_env_wins_over_the_main_repo(tmp_path):
    """워크트리에 따로 두면 그쪽이 이긴다 — 실험용 토큰을 격리할 수 있어야 한다."""
    from devcrew.slack_engine import env_candidates
    main = tmp_path / "repo"
    (main / ".git" / "worktrees" / "wt1").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt1\n")
    cands = env_candidates(wt)
    assert cands.index(wt / ".env") < cands.index(main / ".env")


def test_plain_checkout_has_a_single_candidate(tmp_path):
    """워크트리가 아닌 보통 클론에서는 그 저장소의 .env 하나뿐이다."""
    from devcrew.slack_engine import env_candidates
    (tmp_path / ".git").mkdir()
    assert env_candidates(tmp_path) == [tmp_path / ".env"]


@pytest.mark.asyncio
async def test_tutor_say_keeps_everything_inside_the_thread():
    """회차는 **스레드 안에서만** 돈다 — 채널 피드로 새어 나가지 않는다.

    한때 `reply_broadcast`로 채널에도 함께 게시했다. 모바일에서 회차가 안 보인다는 진단
    때문이었는데, 실제로는 스레드 답글은 PC·모바일 모두 스레드 안에서 정상적으로 보인다 —
    broadcast가 바꾸는 것은 "스레드를 열지 않아도 보이느냐" 하나뿐이었다. 그 한 탭의
    대가로 **정답과 해설이 채널 전체에 게시**됐다 (2026-08-24 최종 리뷰 I7). 회차를 아직
    풀지 않은 사람에게는 스포일러다."""
    from devcrew.slack_engine import make_tutor_say

    class Client:
        def __init__(self):
            self.calls = []

        async def chat_postMessage(self, **kw):
            self.calls.append(kw)
            return {"ts": "600.1"}

    client = Client()
    say = make_tutor_say(client, "C1", "100.1")

    await say(text="문항", blocks=[{"x": 1}])
    kw = client.calls[0]
    assert kw["channel"] == "C1" and kw["thread_ts"] == "100.1"
    assert not kw.get("reply_broadcast"), "채널 피드로 새면 정답·해설이 회차 밖으로 나간다"
    assert kw["blocks"] == [{"x": 1}]

    # 호출자가 스레드를 명시하면 그쪽을 따른다 (기본 thread는 폴백이다)
    await say(text="다른 스레드", thread_ts="200.2")
    assert client.calls[1]["thread_ts"] == "200.2"


@pytest.mark.asyncio
async def test_tutor_say_never_broadcasts_at_all():
    """스레드가 없을 때도 마찬가지다 — broadcast는 어느 경로에서도 켜지지 않는다."""
    from devcrew.slack_engine import make_tutor_say

    class Client:
        def __init__(self):
            self.calls = []

        async def chat_postMessage(self, **kw):
            self.calls.append(kw)
            return {"ts": "600.1"}

    client = Client()
    await make_tutor_say(client, "C1", None)(text="스레드 없음")
    assert not client.calls[0].get("reply_broadcast")


@pytest.mark.asyncio
async def test_tutor_question_router_ignores_bot_messages():
    """tutor 응답은 채널에 broadcast되고 그건 다시 message 이벤트로 돌아온다.
    거르지 않으면 봇이 자기 답변에 답하는 무한 루프가 된다."""
    from devcrew.slack_engine import make_tutor_question

    class H:
        def __init__(self):
            self.calls = []

        async def on_question(self, **kw):
            self.calls.append(kw)

    class Client:
        async def chat_postMessage(self, **kw):
            return {"ts": "1"}

    h = H()
    route = make_tutor_question(h, Client())

    await route({"event": {"type": "message", "bot_id": "B1", "text": "내 답변",
                           "thread_ts": "100.1", "channel": "C1", "user": "U1"}})
    await route({"event": {"type": "message", "subtype": "message_changed",
                           "text": "수정됨", "thread_ts": "100.1",
                           "channel": "C1", "user": "U1"}})
    assert h.calls == []


@pytest.mark.asyncio
async def test_tutor_question_router_forwards_thread_replies():
    from devcrew.slack_engine import make_tutor_question

    class H:
        def __init__(self):
            self.calls = []

        async def on_question(self, **kw):
            self.calls.append(kw)

    class Client:
        async def chat_postMessage(self, **kw):
            return {"ts": "1"}

    h = H()
    await make_tutor_question(h, Client())(
        {"event": {"type": "message", "text": "왜 그런가요?", "thread_ts": "100.1",
                   "ts": "100.9", "channel": "C1", "user": "U-OWNER"}})
    assert h.calls[0]["thread_ts"] == "100.1"
    assert h.calls[0]["user"] == "U-OWNER"
    assert h.calls[0]["text"] == "왜 그런가요?"


@pytest.mark.asyncio
async def test_top_level_message_is_not_a_question():
    """스레드 밖 채널 메시지는 회차와 무관하다 — 건드리지 않는다."""
    from devcrew.slack_engine import make_tutor_question

    class H:
        def __init__(self):
            self.calls = []

        async def on_question(self, **kw):
            self.calls.append(kw)

    class Client:
        async def chat_postMessage(self, **kw):
            return {"ts": "1"}

    h = H()
    await make_tutor_question(h, Client())(
        {"event": {"type": "message", "text": "잡담", "ts": "200.1",
                   "channel": "C1", "user": "U1"}})
    assert h.calls == []


@pytest.mark.asyncio
async def test_mention_strips_the_bot_handle_from_the_question():
    """스레드에서 `@tutor 이거 왜 이래?`가 가장 자연스러운 형태다."""
    from devcrew.slack_engine import make_tutor_question

    class H:
        def __init__(self):
            self.calls = []

        async def on_question(self, **kw):
            self.calls.append(kw)

    class Client:
        async def chat_postMessage(self, **kw):
            return {"ts": "1"}

    h = H()
    await make_tutor_question(h, Client())(
        {"event": {"type": "app_mention", "text": "<@U0BRV19AMLL> 이거 왜 이래?",
                   "thread_ts": "100.1", "ts": "100.9",
                   "channel": "C1", "user": "U-OWNER"}})
    assert h.calls[0]["text"] == "이거 왜 이래?"


@pytest.mark.asyncio
async def test_tutor_question_router_dedupes_same_message_across_event_types():
    """Slack은 스레드 안 "@tutor 질문"을 app_mention과 message 두 이벤트로, 서로 다른
    event_id로 보낸다. event_id로는 걸러지지 않으므로 메시지 자신의 (channel, ts)로
    막아야 한다 — 안 그러면 같은 질문에 답이 두 번 나간다."""
    from devcrew.slack_engine import make_tutor_question

    class H:
        def __init__(self):
            self.calls = []

        async def on_question(self, **kw):
            self.calls.append(kw)

    class Client:
        async def chat_postMessage(self, **kw):
            return {"ts": "1"}

    h = H()
    route = make_tutor_question(h, Client())

    await route({"event_id": "Ev1", "event": {"type": "app_mention",
                 "text": "<@U0BRV19AMLL> 이거 왜 이래?", "thread_ts": "100.1",
                 "ts": "100.9", "channel": "C1", "user": "U-OWNER"}})
    await route({"event_id": "Ev2", "event": {"type": "message",
                 "text": "<@U0BRV19AMLL> 이거 왜 이래?", "thread_ts": "100.1",
                 "ts": "100.9", "channel": "C1", "user": "U-OWNER"}})

    assert len(h.calls) == 1


@pytest.mark.asyncio
async def test_mention_only_strips_leading_bot_handle_not_mentions_inside():
    """`@tutor <@U999>가 왜 여기 나와?`처럼 질문 본문 안에 다른 사람 멘션이 있으면
    그건 질문의 주어다 — 지워버리면 문장이 깨져 복구할 수 없다."""
    from devcrew.slack_engine import make_tutor_question

    class H:
        def __init__(self):
            self.calls = []

        async def on_question(self, **kw):
            self.calls.append(kw)

    class Client:
        async def chat_postMessage(self, **kw):
            return {"ts": "1"}

    h = H()
    await make_tutor_question(h, Client())(
        {"event": {"type": "app_mention",
                   "text": "<@U0BRV19AMLL> <@U999>가 왜 여기 나와?",
                   "thread_ts": "100.1", "ts": "100.9",
                   "channel": "C1", "user": "U-OWNER"}})
    assert h.calls[0]["text"] == "<@U999>가 왜 여기 나와?"


# ── @tutor app_mention 라우팅 분기 (최종 리뷰 I5) ────────────────────────────
class DispatchSpy:
    """회차 유무를 스스로 아는 tutor 대역."""

    def __init__(self, rounds=()):
        self.rounds = set(rounds)
        self.questions = []
        self.mentions = []

    def has_round(self, thread_ts):
        return thread_ts in self.rounds

    async def on_question(self, **kw):
        self.questions.append(kw)

    async def on_mention(self, body, say):
        self.mentions.append(body)


class DispatchClient:
    async def chat_postMessage(self, **kw):
        return {"ts": "1"}


@pytest.mark.asyncio
async def test_tutor_dispatch_routes_an_in_thread_mention_with_a_round_to_a_question():
    """채점이 끝난 스레드의 `@tutor 이거 왜 이래?`는 새 회차가 아니라 질문이다.
    이 결정이 `_amain`의 클로저 안에 있어 어떤 테스트도 닿지 못했다 (lessons C1 —
    이 저장소가 배선을 두 번 조용히 잃은 자리)."""
    from devcrew.slack_engine import make_tutor_dispatch

    t = DispatchSpy(rounds={"100.1"})
    await make_tutor_dispatch(t, DispatchClient())(
        {"event": {"type": "app_mention", "text": "<@U0BRV19AMLL> 이거 왜 이래?",
                   "thread_ts": "100.1", "ts": "100.9", "channel": "C1",
                   "user": "U-OWNER"}})
    assert t.mentions == []
    assert t.questions[0]["thread_ts"] == "100.1"
    assert t.questions[0]["text"] == "이거 왜 이래?"


@pytest.mark.asyncio
async def test_tutor_dispatch_starts_a_round_for_an_in_thread_mention_with_no_round():
    """**모든** in-thread 멘션을 질문으로 보내면 스레드 안에서 `@tutor myrepo:` 로
    회차를 시작하는 길이 사라진다 — `on_question`은 회차가 없으면 조용히 무시하므로
    사용자에게는 무반응이 된다 (최종 리뷰 I5의 부수 회귀)."""
    from devcrew.slack_engine import make_tutor_dispatch

    t = DispatchSpy()
    await make_tutor_dispatch(t, DispatchClient())(
        {"event": {"type": "app_mention", "text": "<@U0BRV19AMLL> myrepo:",
                   "thread_ts": "100.1", "ts": "100.9", "channel": "C1",
                   "user": "U-OWNER"}})
    assert t.questions == []
    assert len(t.mentions) == 1


@pytest.mark.asyncio
async def test_tutor_dispatch_sends_a_top_level_mention_to_on_mention():
    from devcrew.slack_engine import make_tutor_dispatch

    t = DispatchSpy(rounds={"200.1"})
    await make_tutor_dispatch(t, DispatchClient())(
        {"event": {"type": "app_mention", "text": "<@U0BRV19AMLL> myrepo:",
                   "ts": "200.1", "channel": "C1", "user": "U-OWNER"}})
    assert t.questions == [] and len(t.mentions) == 1


@pytest.mark.asyncio
async def test_tutor_dispatch_shares_the_dedupe_state_with_the_message_router():
    """스레드 안의 `@tutor 질문`은 app_mention과 message 두 이벤트로 온다. 분기를
    꺼내면서 라우터를 따로 만들면 중복 차단 상태(`(channel, ts)`)가 갈라져 같은
    질문에 답이 두 번 나간다."""
    from devcrew.slack_engine import make_tutor_dispatch, make_tutor_question

    t = DispatchSpy(rounds={"100.1"})
    client = DispatchClient()
    question = make_tutor_question(t, client)
    dispatch = make_tutor_dispatch(t, client, question)

    ev = {"text": "<@U0BRV19AMLL> 이거 왜 이래?", "thread_ts": "100.1",
          "ts": "100.9", "channel": "C1", "user": "U-OWNER"}
    await dispatch({"event_id": "Ev1", "event": {**ev, "type": "app_mention"}})
    await question({"event_id": "Ev2", "event": {**ev, "type": "message"}})

    assert len(t.questions) == 1


def test_engine_starts_the_tutor_sweep_loop():
    """엔진이 `sweep_loop`를 task로 띄우는지 **소스에서** 확인한다.

    이 배선은 네트워크·Slack 토큰이 있어야 도는 자리에 있어 함수로 부를 seam이 없다.
    그래서 약한 테스트다 — 루프가 실제로 도는 것은 `test_slack_tutor.py`가 따로 잡고,
    여기서 잡는 것은 **그 줄이 사라지는 것** 하나다. 그것만으로 충분한 이유는, 이
    저장소가 배선을 잃은 세 번이 전부 "코드는 멀쩡한데 부르는 자리가 없어진" 경우였기
    때문이다 (lessons C1).
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "devcrew" / "slack_engine.py"
    body = src.read_text()
    assert "sweep_loop" in body, "sweep_loop 배선이 사라졌다"
    assert re.search(r"tasks\.append\(\s*sweep_loop\(", body), \
        "sweep_loop가 실행 task로 등록되지 않았다 — import만 남으면 돌지 않는다"


# ── 미등록 repo → leader 판단 → 생성 ─────────────────────────────────────────
def _repo_env(tmp_path, monkeypatch):
    """workspace_roots와 registry 로더를 임시 디렉토리로 돌린다 (실제 ~/Desktop 금지)."""
    import functools

    import devcrew.repos as repos_mod
    import devcrew.slack_engine as se

    ws = tmp_path / "workspace"
    ws.mkdir()
    y = tmp_path / "repos.yaml"
    y.write_text(f"workspace_roots:\n  - {ws}\n")
    monkeypatch.setattr(se, "workspace_roots", lambda: [ws])
    monkeypatch.setattr(se, "load_repos", functools.partial(repos_mod.load_repos, y))
    monkeypatch.setattr(se, "load_repo_bases", functools.partial(repos_mod.load_repo_bases, y))
    return ws


def _leader(action, rationale="이유"):
    """make_llm_decide 대역 — 지정한 action을 내는 decide를 돌려준다."""
    opened = []

    def factory(orch, cfg, *, mcp_servers=None, leader_state=None, **kw):
        async def decide(trigger, snapshot):
            opened.append({"trigger": trigger, "snapshot": snapshot,
                           "leader_state": leader_state})
            if leader_state is not None:            # 세션이 열린 것처럼 흉내낸다
                leader_state.update({"inst": None, "sid": None})
            return {"action": action, "target_node": None, "rationale": rationale}, "ORCH-1", 7
        return decide

    factory.opened = opened
    return factory


@pytest.mark.asyncio
async def test_create_repo_decision_makes_the_repo_and_runs(tmp_path, monkeypatch):
    """leader가 CREATE_REPO를 내면 엔진이 만들고, 그 자리에서 실행이 이어져야 한다."""
    import devcrew.decision as decision_mod
    import devcrew.engine as engine_mod
    from devcrew.slack_engine import EngineRunner

    ws = _repo_env(tmp_path, monkeypatch)
    seen: list[dict] = []

    class RecordingEngine:
        def __init__(self, orch, cfg, **kw):
            pass

        async def run(self, *, execution_id, task, worktree=None, carry=None):
            seen.append({"task": task, "worktree": worktree})
            return engine_mod.ExecutionResult("COMPLETED", [], 0, 0)

    monkeypatch.setattr(engine_mod, "WorkflowEngine", RecordingEngine)
    leader = _leader("CREATE_REPO")
    monkeypatch.setattr(decision_mod, "make_llm_decide", leader)

    runner = EngineRunner(runtime_dir=tmp_path / "rt")
    _eid, _res, repo = await runner.run("todo-web: 할 일 앱 만들어줘")

    assert (ws / "todo-web" / ".git").exists()
    assert repo.startswith("todo-web · wt/slack-")
    # 접두는 떨어지고 본문만 워커에게 간다
    assert seen[0]["task"] == "할 일 앱 만들어줘"
    # worktree가 새 repo 안에서 잡혔다 — 커밋이 없었으면 여기서 실패한다
    assert str(ws / "todo-web") in seen[0]["worktree"]
    # leader에게 판단 근거가 실제로 갔는가
    snap = leader.opened[0]["snapshot"]
    assert leader.opened[0]["trigger"] == "UNKNOWN_REPO"
    assert snap["requested_repo"] == "todo-web"
    assert snap["task"] == "todo-web: 할 일 앱 만들어줘"
    assert snap["allowed_actions"] == ["CREATE_REPO", "ASK_USER", "ABORT"]


@pytest.mark.asyncio
async def test_ask_user_decision_raises_with_the_rationale(tmp_path, monkeypatch):
    """확신 없으면 만들지 않는다 — 사람이 버튼으로 정하게 rationale을 실어 올린다."""
    import devcrew.decision as decision_mod
    from devcrew.slack_engine import EngineRunner, RepoAskUser

    ws = _repo_env(tmp_path, monkeypatch)
    monkeypatch.setattr(decision_mod, "make_llm_decide",
                        _leader("ASK_USER", "dev-crew 오타로 보입니다"))

    runner = EngineRunner(runtime_dir=tmp_path / "rt")
    with pytest.raises(RepoAskUser) as ei:
        await runner.run("dev-crw: 로그 고쳐")
    assert ei.value.name == "dev-crw"
    assert "오타" in ei.value.rationale
    assert list(ws.iterdir()) == []            # 아무것도 만들지 않았다


@pytest.mark.asyncio
async def test_abort_decision_creates_nothing(tmp_path, monkeypatch):
    import devcrew.decision as decision_mod
    from devcrew.repos import RepoRegistryError
    from devcrew.slack_engine import EngineRunner, RepoAskUser

    ws = _repo_env(tmp_path, monkeypatch)
    monkeypatch.setattr(decision_mod, "make_llm_decide", _leader("ABORT", "요청이 불명확"))

    runner = EngineRunner(runtime_dir=tmp_path / "rt")
    with pytest.raises(RepoRegistryError) as ei:
        await runner.run("nope: 뭔가 해줘")
    assert not isinstance(ei.value, RepoAskUser)   # 버튼을 띄우지 않는다
    assert list(ws.iterdir()) == []


# ── 생성 버튼 클릭 ───────────────────────────────────────────────────────────
class _ClickRunner:
    """handle_create_repo_click이 러너에게 기대하는 표면만 가진 대역."""

    def __init__(self, *, fail: Exception | None = None):
        self.pending: dict[str, dict] = {}
        self.created: list[str] = []
        self.ran: list[str] = []
        self.fail = fail

    def pending_repo(self, key):
        return self.pending.get(key)

    def pop_pending_repo(self, key):
        return self.pending.pop(key, None)

    def create_repo_now(self, name):
        if self.fail:
            raise self.fail
        self.created.append(name)
        return f"/ws/{name}"

    async def run(self, task, **kw):
        self.ran.append(task)
        return "SLACK-9", FakeResult(), "todo-web · wt/slack-9"


@pytest.mark.asyncio
async def test_click_creates_and_reruns_the_original_task():
    from devcrew.slack_engine import handle_create_repo_click

    r = _ClickRunner()
    r.pending["K"] = {"name": "todo-web", "task": "todo-web: 만들어줘", "user": "U1"}
    posted: list[str] = []

    status = await handle_create_repo_click(
        r, key="K", user="U1", post=lambda t: _collect(posted, t))
    assert r.created == ["todo-web"]
    assert r.ran == ["todo-web: 만들어줘"]      # 접두 포함 원문 — 이제 registry에 있다
    assert "생성됨" in status
    assert "K" not in r.pending                  # 중복 클릭 방지


@pytest.mark.asyncio
async def test_click_by_a_stranger_creates_nothing_and_keeps_the_button():
    from devcrew.slack_engine import handle_create_repo_click

    r = _ClickRunner()
    r.pending["K"] = {"name": "todo-web", "task": "t", "user": "U1"}
    posted: list[str] = []

    status = await handle_create_repo_click(
        r, key="K", user="U2", post=lambda t: _collect(posted, t))
    assert r.created == [] and r.ran == []
    assert status == ""                          # 빈 문자열 = 버튼을 걷지 않는다
    assert "K" in r.pending                      # 주인이 아직 누를 수 있다
    assert any("요청을 보낸 사용자만" in p for p in posted)


@pytest.mark.asyncio
async def test_click_on_an_expired_key_says_so():
    from devcrew.slack_engine import handle_create_repo_click

    r = _ClickRunner()
    status = await handle_create_repo_click(r, key="GONE", user="U1",
                                            post=lambda t: _collect([], t))
    assert "만료" in status and r.created == []


@pytest.mark.asyncio
async def test_click_reports_creation_failure_without_running():
    from devcrew.repos import RepoRegistryError
    from devcrew.slack_engine import handle_create_repo_click

    r = _ClickRunner(fail=RepoRegistryError("이미 있다"))
    r.pending["K"] = {"name": "todo-web", "task": "t", "user": "U1"}
    posted: list[str] = []

    status = await handle_create_repo_click(
        r, key="K", user="U1", post=lambda t: _collect(posted, t))
    assert r.ran == []                           # 만들지 못했으면 실행하지 않는다
    assert "실패" in status
    assert any("이미 있다" in p for p in posted)


async def _collect(sink: list, text: str) -> None:
    sink.append(text)


@pytest.mark.asyncio
async def test_preflight_leader_session_is_reclaimed(tmp_path, monkeypatch):
    """일회성 leader 세션을 안 걷으면 미등록 repo 요청 하나마다 워커가 하나씩 남는다.

    tutor에서 17시간 산 워커를 실제로 발견한 그 실수다 (lessons C13).
    """
    import devcrew.decision as decision_mod
    from devcrew.schema import Provider
    from devcrew.slack_engine import EngineRunner, RepoAskUser

    _repo_env(tmp_path, monkeypatch)
    fake_inst = type("I", (), {"provider": Provider.CLAUDE_CODE, "instance_id": "ORCH-1"})()

    def factory(orch, cfg, *, mcp_servers=None, leader_state=None, **kw):
        async def decide(trigger, snapshot):
            leader_state.update({"inst": fake_inst, "sid": "sess-1"})
            return {"action": "ASK_USER", "target_node": None, "rationale": "애매"}, "ORCH-1", 1
        return decide

    monkeypatch.setattr(decision_mod, "make_llm_decide", factory)

    runner = EngineRunner(runtime_dir=tmp_path / "rt")
    archived: list[str] = []
    finished: list[str] = []

    class FakeAdapter:
        async def archive(self, sid):
            archived.append(sid)

    runner.orch.adapters[Provider.CLAUDE_CODE] = FakeAdapter()
    monkeypatch.setattr(runner.orch.registry, "finish", lambda iid: finished.append(iid))

    with pytest.raises(RepoAskUser):
        await runner.run("todo-web: 만들어줘")
    assert archived == ["sess-1"]
    assert finished == ["ORCH-1"]
