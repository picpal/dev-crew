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
