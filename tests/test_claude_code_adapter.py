"""finding #8 회귀 — start_session -> resume 경로에서 ClaudeAgentOptions(role
enforcement/구조화 출력 강제/harness MCP)가 동일하게 복원되는지 확인한다.

실 LLM 호출은 절대 하지 않는다: `claude_code.ClaudeSDKClient`를 대역으로
monkeypatch해 connect()/query()/receive_response()가 네트워크에 닿지 않게 막고,
그 대역에 실제로 전달된 ClaudeAgentOptions 인스턴스를 그대로 관찰한다
(claude_code.py의 resume()이 캐시해 둔 시작 설정을 실제로 재주입하는지 검증하려면
SDK가 받는 옵션 객체 자체를 봐야 한다 — mock이 아니라 실제 dataclass 값 비교).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from devcrew.adapters import claude_code as claude_code_module
from devcrew.adapters.base import ResumeConfigMissingError
from devcrew.adapters.claude_code import ClaudeCodeAdapter
from devcrew.schema import AgentInstance, EffortLevel, Provider, Role
from devcrew.store.registry import SessionRegistry
from devcrew.store.trace import TraceStore

pytestmark = pytest.mark.asyncio


class AssistantMessage:
    """claude_code.py의 _turn()은 `type(msg).__name__`로 메시지 종류를 판별한다 —
    이 대역 클래스 이름이 실제 SDK 타입명과 일치해야 그 분기를 태울 수 있다."""

    def __init__(self, text: str):
        self.content = [SimpleNamespace(text=text)]


class ResultMessage:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.usage = {"input_tokens": 1, "output_tokens": 1}
        self.total_cost_usd = 0.0
        self.model_usage = {}
        self.structured_output = {
            "status": "PASS", "summary": "ok", "changed_files": [],
            "build": {"ok": True, "detail": ""},
            "tests": {"passed": 0, "failed": 0, "detail": ""}}


class FakeSDKClient:
    """ClaudeSDKClient(options) 대역. 생성 시 넘어온 options를 그대로 보존해
    start_session/resume이 SDK에 실제로 어떤 설정을 넘기는지 관찰할 수 있게 한다."""

    captured_options: list = []

    def __init__(self, options):
        self.options = options
        FakeSDKClient.captured_options.append(options)

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def query(self, message):
        self._last_message = message

    async def receive_response(self):
        yield AssistantMessage("ok")
        yield ResultMessage(session_id="claude-sess-1")

    async def interrupt(self):
        return None


@pytest.fixture(autouse=True)
def _reset_captured_options():
    FakeSDKClient.captured_options = []
    yield


def make_adapter(tmp_path, monkeypatch) -> ClaudeCodeAdapter:
    monkeypatch.setattr(claude_code_module, "ClaudeSDKClient", FakeSDKClient)
    trace = TraceStore(tmp_path / "trace.db")
    registry = SessionRegistry(tmp_path / "harness.db")
    return ClaudeCodeAdapter(trace, registry)


def make_inst() -> AgentInstance:
    return AgentInstance(
        instance_id="DEV-1", role=Role.DEVELOPER, provider=Provider.CLAUDE_CODE,
        adapter="claude-code-adapter", model="claude-sonnet-5",
        effort_level=EffortLevel.HIGH, reasoning_level=None,
        routing_policy_version="v1", routing_reason="test",
        session_id=None, execution_id="E1", workflow_id="E1", node_id="n1",
        task_scope="*", worktree="/tmp/work")


async def test_resume_restores_start_session_options(tmp_path, monkeypatch):
    adapter = make_adapter(tmp_path, monkeypatch)
    inst = make_inst()
    mcp_servers = {"harness": {"type": "sdk", "name": "harness"}}

    session_id = await adapter.start_session(
        inst, "시작", system_prompt="SYSTEM PROMPT", output_schema={"type": "object"},
        mcp_servers=mcp_servers)
    assert session_id == "claude-sess-1"
    assert len(FakeSDKClient.captured_options) == 1
    start_opts = FakeSDKClient.captured_options[0]
    # start_session 시점에 role enforcement/구조화 출력/harness MCP가 실제로 SDK에
    # 전달됐는지 먼저 확인 — 이게 틀리면 resume 비교가 무의미하다.
    assert start_opts.system_prompt == "SYSTEM PROMPT"
    assert start_opts.output_format == {"type": "json_schema", "schema": {"type": "object"}}
    assert start_opts.mcp_servers == mcp_servers
    assert start_opts.allowed_tools           # DEVELOPER role policy의 allowlist
    assert start_opts.cwd == inst.worktree

    await adapter.resume(session_id, "재개")
    assert len(FakeSDKClient.captured_options) == 2
    resume_opts = FakeSDKClient.captured_options[1]

    # finding #8 — resume()이 start_session의 시작 설정을 실제로 재주입했는지 검증.
    assert resume_opts.resume == session_id
    assert resume_opts.mcp_servers == start_opts.mcp_servers == mcp_servers
    assert resume_opts.allowed_tools == start_opts.allowed_tools
    assert resume_opts.permission_mode == start_opts.permission_mode
    assert resume_opts.system_prompt == start_opts.system_prompt == "SYSTEM PROMPT"
    assert resume_opts.output_format == start_opts.output_format == \
        {"type": "json_schema", "schema": {"type": "object"}}
    assert resume_opts.cwd == start_opts.cwd == inst.worktree
    assert resume_opts.model == start_opts.model == inst.model
    assert resume_opts.effort == start_opts.effort == "high"


async def test_resume_without_output_schema_restores_none_output_format(tmp_path, monkeypatch):
    """output_schema가 None으로 시작된 세션(예: 결정 세션 외 일부 경로)은 resume에서도
    output_format이 None으로 유지돼야 한다 — 이전 세션의 schema가 새어들지 않는다."""
    adapter = make_adapter(tmp_path, monkeypatch)
    inst = make_inst()
    session_id = await adapter.start_session(inst, "시작", system_prompt="P",
                                             output_schema=None, mcp_servers=None)
    start_opts = FakeSDKClient.captured_options[0]
    assert start_opts.output_format is None
    assert start_opts.mcp_servers == {}

    await adapter.resume(session_id, "재개")
    resume_opts = FakeSDKClient.captured_options[1]
    assert resume_opts.output_format is None
    assert resume_opts.mcp_servers == {}


async def test_resume_without_cached_config_fails_closed(tmp_path, monkeypatch):
    """start_session을 거치지 않은(예: cross-process 재시작 후 새 adapter 인스턴스)
    session_id로 resume하면 기본적으로 ResumeConfigMissingError로 fail-closed한다."""
    adapter = make_adapter(tmp_path, monkeypatch)
    with pytest.raises(ResumeConfigMissingError):
        await adapter.resume("unknown-session", "재개")


async def test_resume_allow_unconfigured_bypasses_fail_closed(tmp_path, monkeypatch):
    """allow_unconfigured=True는 의도적으로 캐시 없는 resume을 허용한다 (deferred B1
    cross-process recovery 경로) — enforcement 없이 SDK 기본값으로 resume된다."""
    adapter = make_adapter(tmp_path, monkeypatch)
    outcome = await adapter.resume("unknown-session", "재개", allow_unconfigured=True)
    assert outcome.text == "ok"
    resume_opts = FakeSDKClient.captured_options[0]
    assert resume_opts.resume == "unknown-session"
    assert resume_opts.system_prompt is None
