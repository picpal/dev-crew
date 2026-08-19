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
    def __init__(self, *, error: Exception | None = None, timeout: bool = False):
        self.error, self.timeout_flag = error, timeout
        self.timeout = 600.0
        self.busy = False
        self.calls: list[str] = []

    async def run(self, task: str, *, on_progress=None):
        self.calls.append(task)
        if self.timeout_flag:
            raise asyncio.TimeoutError
        if self.error:
            raise self.error
        return "SLACK-1", FakeResult(), "/tmp/repo"


class SaySpy:
    def __init__(self):
        self.messages: list[dict] = []

    async def __call__(self, *, text: str, thread_ts=None):
        self.messages.append({"text": text, "thread_ts": thread_ts})


def body(text: str, event_id: str = "Ev1", ts: str = "111.222") -> dict:
    return {"event_id": event_id, "event": {"text": text, "ts": ts}}


@pytest.mark.asyncio
async def test_mention_runs_task_and_replies_in_thread():
    runner, say = FakeRunner(), SaySpy()
    await MentionHandler(runner)(body("<@U123> calc.py에 mul 추가"), say)
    assert runner.calls == ["calc.py에 mul 추가"]           # mention 제거된 task
    assert len(say.messages) == 2                            # 접수 + 결과
    assert say.messages[0]["text"].startswith("⏳ 접수")
    assert "✅ SLACK-1 COMPLETED" in say.messages[1]["text"]
    assert all(m["thread_ts"] == "111.222" for m in say.messages)


@pytest.mark.asyncio
async def test_duplicate_event_id_is_ignored():
    runner, say = FakeRunner(), SaySpy()
    h = MentionHandler(runner)
    await h(body("<@U123> t", event_id="EvX"), say)
    await h(body("<@U123> t", event_id="EvX"), say)           # Slack 재전송
    assert runner.calls == ["t"]                              # 1회만 실행
    assert len(say.messages) == 2


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
    assert text.startswith("🙋 SLACK-2 NEEDS_HUMAN")
    assert "develop:PASS → review:PASS" in text
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
    assert "실행 중 (32s)" in t and "현재: REVIEWER" in t
    assert "develop:PASS" in t and "결정 1회" in t
