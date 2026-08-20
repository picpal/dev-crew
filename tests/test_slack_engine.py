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
