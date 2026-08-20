"""slack_brain — 인터뷰 세션·핸드오프 로직 (FakeAdapter, LLM·Slack 없음)."""
import pytest

from devcrew.adapters.base import FakeAdapter
from devcrew.config import load as load_config
from devcrew.orchestrator import Orchestrator
from devcrew.schema import Provider
from devcrew.slack_brain import BrainHandler, brief_to_task
from devcrew.store.registry import SessionRegistry
from devcrew.store.trace import TraceStore

BRIEF_PASS = {"status": "PASS", "summary": "알림 기능 brief", "goal": "결제 알림 발송",
              "decisions": ["웹훅 방식"], "constraints": ["외부 SDK 금지"],
              "acceptance_criteria": ["알림 1건 발송 검증"], "target_repo": None,
              "open_questions": []}
BRIEF_BLOCKED = {**BRIEF_PASS, "status": "BLOCKED", "summary": "전송 채널 미결정",
                 "open_questions": ["채널: 이메일 vs 슬랙?"]}


class DispatchSpy:
    def __init__(self):
        self.calls = []

    async def __call__(self, task, channel, interview_ts, brief_text):
        self.calls.append({"task": task, "channel": channel, "ts": interview_ts})


class SaySpy:
    def __init__(self):
        self.messages = []

    async def __call__(self, *, text, thread_ts=None):
        self.messages.append({"text": text, "thread_ts": thread_ts})


def make_handler(tmp_path, *, brief=BRIEF_PASS, repos=None, dispatch=None):
    trace = TraceStore(tmp_path / "t.db")
    registry = SessionRegistry(tmp_path / "h.db")
    fake = FakeAdapter(script=["질문1: 범위는? (권장: 최소)", "질문2: 채널은?", "질문3"],
                       structured_script=[brief, brief, brief])
    orch = Orchestrator(trace, registry, {Provider.CLAUDE_CODE: fake,
                                          Provider.CODEX: fake})
    dispatch = dispatch or DispatchSpy()
    h = BrainHandler(orch, load_config(), repos or {}, dispatch)
    return h, dispatch, fake


def mention(text, ts="100.1", event_id="EvB1"):
    return {"event_id": event_id,
            "event": {"text": text, "ts": ts, "channel": "C1"}}


def reply(text, thread_ts="100.1", event_id="EvR1", bot=False):
    e = {"text": text, "thread_ts": thread_ts, "channel": "C1", "ts": "101.1"}
    if bot:
        e["bot_id"] = "B999"
    return {"event_id": event_id, "event": e}


@pytest.mark.asyncio
async def test_mention_starts_interview_session(tmp_path):
    h, _, _ = make_handler(tmp_path)
    say = SaySpy()
    await h.on_mention(mention("<@U1> 결제 알림 기능"), say)
    assert "100.1" in h.sessions
    sess = h.sessions["100.1"]
    assert sess.transcript[0] == "[사용자] 결제 알림 기능"
    assert "질문1" in say.messages[0]["text"]
    assert say.messages[0]["thread_ts"] == "100.1"


@pytest.mark.asyncio
async def test_thread_reply_continues_session(tmp_path):
    h, _, _ = make_handler(tmp_path)
    say = SaySpy()
    await h.on_mention(mention("<@U1> 결제 알림"), say)
    await h.on_thread_message(reply("최소 범위로", event_id="EvR2"), say)
    sess = h.sessions["100.1"]
    assert "[사용자] 최소 범위로" in sess.transcript
    assert "질문2" in say.messages[-1]["text"]


@pytest.mark.asyncio
async def test_bot_messages_ignored(tmp_path):
    h, _, _ = make_handler(tmp_path)
    say = SaySpy()
    await h.on_mention(mention("<@U1> 결제 알림"), say)
    before = len(h.sessions["100.1"].transcript)
    await h.on_thread_message(reply("봇이 쓴 글", event_id="EvR3", bot=True), say)
    assert len(h.sessions["100.1"].transcript) == before


@pytest.mark.asyncio
async def test_handoff_keyword_dispatches_to_crew_and_closes_session(tmp_path):
    h, dispatch, _ = make_handler(tmp_path)
    say = SaySpy()
    await h.on_mention(mention("<@U1> 결제 알림"), say)
    await h.on_thread_message(reply("좋아, 전달해줘", event_id="EvR4"), say)
    assert len(dispatch.calls) == 1
    assert "결제 알림 발송" in dispatch.calls[0]["task"]
    assert dispatch.calls[0]["channel"] == "C1"
    assert "100.1" not in h.sessions                     # 세션 정리됨
    assert any("brief 확정" in m["text"] for m in say.messages)


@pytest.mark.asyncio
async def test_blocked_brief_keeps_session_no_dispatch(tmp_path):
    h, dispatch, _ = make_handler(tmp_path, brief=BRIEF_BLOCKED)
    say = SaySpy()
    await h.on_mention(mention("<@U1> 결제 알림"), say)
    await h.on_thread_message(reply("전달", event_id="EvR5"), say)
    assert dispatch.calls == []
    assert "100.1" in h.sessions                         # 계속 인터뷰 가능
    assert any("아직 전달할 수준이 아닙니다" in m["text"] for m in say.messages)


@pytest.mark.asyncio
async def test_duplicate_event_ignored(tmp_path):
    h, _, _ = make_handler(tmp_path)
    say = SaySpy()
    await h.on_mention(mention("<@U1> 결제 알림", event_id="EvDup"), say)
    await h.on_mention(mention("<@U1> 결제 알림", event_id="EvDup"), say)
    assert len(say.messages) == 1


def test_brief_to_task_includes_repo_prefix():
    t = brief_to_task({**BRIEF_PASS, "target_repo": "work-note"}, None)
    assert t.startswith("work-note: 결제 알림 발송")
    assert "수용 기준" in t


def test_brief_to_task_falls_back_to_session_repo():
    assert brief_to_task(BRIEF_PASS, "message-gate").startswith("message-gate: ")


@pytest.mark.asyncio
async def test_long_reply_becomes_report_link(tmp_path):
    long_answer = "긴 검토 내용입니다. " * 60          # > REPORT_THRESHOLD
    trace_dir = tmp_path
    from devcrew.store.trace import TraceStore
    from devcrew.store.registry import SessionRegistry
    from devcrew.orchestrator import Orchestrator
    from devcrew.adapters.base import FakeAdapter
    from devcrew.schema import Provider
    from devcrew.config import load as load_config
    fake = FakeAdapter(script=[long_answer], structured_script=[BRIEF_PASS])
    orch = Orchestrator(TraceStore(trace_dir / "t.db"), SessionRegistry(trace_dir / "h.db"),
                        {Provider.CLAUDE_CODE: fake, Provider.CODEX: fake})
    published = {}
    def publish(task_id, html):
        published["id"], published["html"] = task_id, html
        return f"https://reports.example/tasks/{task_id}"
    h = BrainHandler(orch, load_config(), {}, DispatchSpy(), publish=publish)
    say = SaySpy()
    await h.on_mention(mention("<@U1> 대규모 구조 변경"), say)
    msg = say.messages[0]["text"]
    assert "📄 전체 응답: https://reports.example/tasks/brain-" in msg
    assert len(published["html"]) > 0 and published["id"].startswith("brain-")
    assert "<script" not in published["html"]


@pytest.mark.asyncio
async def test_brief_report_link_in_handoff(tmp_path):
    def publish(task_id, html):
        return f"https://reports.example/tasks/{task_id}"
    h, dispatch, _ = make_handler(tmp_path)
    h.publish = publish
    say = SaySpy()
    await h.on_mention(mention("<@U1> 결제 알림"), say)
    await h.on_thread_message(reply("전달", event_id="EvRep"), say)
    assert any("📄 리포트: https://reports.example/tasks/brain-" in m["text"]
               for m in say.messages)


def test_md_lite_escapes_and_structures():
    from devcrew.report.brain_report import md_lite
    out = md_lite("## 제목\n- 항목 **강조** `코드`\n<script>alert(1)</script>")
    assert "<h2>제목</h2>" in out and "<strong>강조</strong>" in out
    assert "<code>코드</code>" in out
    assert "<script>" not in out and "&lt;script&gt;" in out


def test_parse_options_and_blocks():
    from devcrew.slack_brain import parse_options, question_blocks
    text = "범위를 정하죠.\nA) 최소 범위 (권장)\nB) 전체 재설계\n이유: ..."
    opts = parse_options(text)
    assert opts == ["A) 최소 범위 (권장)", "B) 전체 재설계"]
    blocks = question_blocks(text, opts, decided=2)
    assert blocks[0]["type"] == "header" and "범위를 정하죠" in blocks[0]["text"]["text"]
    actions = next(b for b in blocks if b["type"] == "actions")
    btns = actions["elements"]
    assert btns[0]["style"] == "primary" and "style" not in btns[1]
    assert btns[0]["value"] == "A) 최소 범위 (권장)"
    assert btns[0]["text"]["text"] == "A 선택 ★"              # 짧은 라벨
    detail = [b for b in blocks if b["type"] == "section"]
    assert any("전체 재설계" in b["text"]["text"] for b in detail)  # 선택지 상세는 본문에
    ctx = next(b for b in blocks if b["type"] == "context")
    assert "닫힌 결정 2개" in ctx["elements"][0]["text"]
    assert parse_options("A) 하나뿐") == []                    # 1개는 무효


@pytest.mark.asyncio
async def test_button_answer_feeds_session(tmp_path):
    h, _, _ = make_handler(tmp_path)
    say = SaySpy()
    await h.on_mention(mention("<@U1> 결제 알림"), say)
    stripped = []
    async def strip():
        stripped.append(True)
    await h.on_answer(thread_ts="100.1", value="A) 최소 범위 (권장)", say=say, strip=strip)
    sess = h.sessions["100.1"]
    assert "[사용자] A) 최소 범위 (권장)" in sess.transcript
    assert stripped == [True]
    assert "질문2" in say.messages[-1]["text"]


@pytest.mark.asyncio
async def test_report_form_marker_accepted_from_bot(tmp_path):
    from devcrew.slack_brain import ANSWER_MARKER
    h, _, _ = make_handler(tmp_path)
    say = SaySpy()
    await h.on_mention(mention("<@U1> 결제 알림"), say)
    await h.on_thread_message(reply(f"{ANSWER_MARKER} B) 전체 재설계",
                                    event_id="EvMk", bot=True), say)
    assert "[사용자] B) 전체 재설계" in h.sessions["100.1"].transcript


def test_to_mrkdwn_conversion():
    from devcrew.slack_brain import to_mrkdwn
    src = "## 범위 결정\n**핵심**은 이것.\n* 항목1\n- 항목2\n`code` 유지"
    out = to_mrkdwn(src)
    assert "*범위 결정*" in out and "##" not in out
    assert "*핵심*은" in out and "**" not in out
    assert "- 항목1" in out and "`code`" in out


def test_question_blocks_preserves_paragraph_breaks():
    from devcrew.slack_brain import parse_options, question_blocks
    text = ("요청 분류: architectural\n\n질문 1은 무엇으로 할까요?\n\n맥락 설명입니다.\n\n"
            "A) 로컬 저장 (권장)\nB) 서버 동기화\n\n*권장 이유:* A가 요구에 맞음")
    blocks = question_blocks(text, parse_options(text))
    ctx = blocks[1]["text"]["text"]
    assert "architectural\n\n맥락 설명입니다.\n\n*권장 이유:*" in ctx  # 빈 줄 보존
