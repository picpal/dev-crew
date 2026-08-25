import pytest
from devcrew.adapters.base import FakeAdapter, TurnOutcome
from tests.test_trace import make_inst


async def test_fake_adapter_contract():
    fa = FakeAdapter(script=["first", "second"])
    sid = await fa.start_session(make_inst(), "hello")
    assert sid.startswith("fake-")
    out = await fa.send(sid, "msg")
    assert isinstance(out, TurnOutcome) and out.text == "first"
    out = await fa.send(sid, "msg")
    assert out.text == "second"
    assert (await fa.get_usage(sid)).output_tokens == 2   # turn 수 == output_tokens 규약
    assert await fa.cancel(sid) == "CANCELLED"
    assert await fa.archive(sid) == "ARCHIVED"


async def test_fake_adapter_fail_after():
    fa = FakeAdapter(script=["ok"], fail_after=1)
    sid = await fa.start_session(make_inst(), "x")
    await fa.send(sid, "1")
    with pytest.raises(RuntimeError):
        await fa.send(sid, "2")


async def test_fake_adapter_structured_output():
    fa = FakeAdapter(structured_script=[{"status": "PASS", "summary": "ok"}])
    sid = await fa.start_session(make_inst(), "go",
                                 system_prompt="지침", output_schema={"type": "object"})
    out = await fa.send(sid, "final")
    assert out.structured == {"status": "PASS", "summary": "ok"}
    assert fa.last_system_prompt == "지침"          # 주입 확인용 기록
    assert fa.last_output_schema == {"type": "object"}


async def test_fake_adapter_gives_no_structured_output_without_a_schema():
    """스키마 없이 연 세션은 구조화 출력을 내지 않는다 — 실 어댑터의 계약이다.

    `output_schema`가 없으면 `--json-schema`가 CLI에 붙지 않고, 그런 세션은
    `structured_output`을 **절대** 내지 않는다 (claude_code.py의 output_format 분기,
    codex.py의 `if schema and ...`). 대역이 이 계약을 어기면 "대화형 세션을 열어 놓고
    `out.structured`를 읽는" 결함류를 unit이 영원히 못 잡는다 — 실제로 후속 질문
    기능이 그 이유로 프로덕션에서 한 번도 동작하지 않았다 (2026-08-24 C1).
    """
    fa = FakeAdapter(script=["자연어 답"],
                     structured_script=[{"status": "PASS", "summary": "ok"}])
    sid = await fa.start_session(make_inst(), "go")          # output_schema 없음
    out = await fa.send(sid, "질문")
    assert out.structured is None
    assert out.text == "자연어 답"        # 자연어 turn 자체는 정상이다


async def test_fake_adapter_scopes_the_schema_contract_per_session():
    """한 어댑터가 스키마 있는 세션과 없는 세션을 동시에 들고 있어도 서로 섞이지 않는다."""
    fa = FakeAdapter(structured_script=[{"status": "PASS", "summary": "ok"}])
    plain = await fa.start_session(make_inst(), "go")
    schemad = await fa.start_session(make_inst(), "go",
                                     output_schema={"type": "object"})
    assert (await fa.send(plain, "x")).structured is None
    assert (await fa.send(schemad, "x")).structured == {"status": "PASS", "summary": "ok"}
