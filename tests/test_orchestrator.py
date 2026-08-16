from devcrew.adapters.base import FakeAdapter
from devcrew.orchestrator import Orchestrator, ReviewQueue
from devcrew.schema import EffortLevel, Provider, Role
from devcrew.store.registry import SessionRegistry
from devcrew.store.trace import TraceStore


def make_orch(tmp_path, fake=None):
    trace = TraceStore(tmp_path / "trace.db")
    reg = SessionRegistry(tmp_path / "harness.db")
    fake = fake or FakeAdapter()
    return Orchestrator(trace, reg, {Provider.CLAUDE_CODE: fake, Provider.CODEX: fake}), trace, reg


async def test_spawn_records_routing_event(tmp_path):
    orch, trace, reg = make_orch(tmp_path)
    inst = await orch.spawn(Role.DEVELOPER, "DEFAULT", execution_id="E1",
                            node_id="n1", task_scope="*")
    assert inst.model == "claude-sonnet-5" and inst.effort_level == EffortLevel.HIGH
    evs = trace.events(event_type="ModelRoutingEvent")
    assert evs[0]["payload"]["selected_tier"] == "DEFAULT"
    assert reg.active()[0]["instance_id"] == inst.instance_id


async def test_escalation_spawns_new_instance_with_handoff(tmp_path):
    orch, trace, reg = make_orch(tmp_path)
    dev1 = await orch.spawn(Role.DEVELOPER, "DEFAULT", execution_id="E1",
                            node_id="n1", task_scope="*")
    reg.checkpoint(dev1.instance_id, summary="시도 1 실패", artifacts=["a.py"])
    dev2 = await orch.spawn(Role.DEVELOPER, "HIGH_CAPABILITY", execution_id="E1",
                            node_id="n1", task_scope="*",
                            replaced=dev1, escalation_reason="NEED_REPLAN")
    assert dev2.replaced_instance_id == dev1.instance_id
    assert dev2.model == "claude-opus-5"
    esc = trace.events(event_type="ModelEscalationEvent")[0]["payload"]
    assert esc["reason"] == "NEED_REPLAN"
    assert esc["handoff"]["last_summary"] == "시도 1 실패"
    # 기존 instance는 registry에서 제거되고 종료 상태 보존(§7.6, trace projection)
    assert [r["instance_id"] for r in reg.active()] == [dev2.instance_id]


async def test_review_loop_bounded_and_escalates_on_no_progress(tmp_path):
    orch, trace, _ = make_orch(tmp_path)
    dev = await orch.spawn(Role.DEVELOPER, "DEFAULT", execution_id="E1",
                           node_id="n1", task_scope="*")
    finding = {"n": 0}

    async def review(_): return "NOT_PASS: same-finding"     # 항상 같은 finding
    async def fix(_): finding["n"] += 1

    result = await orch.run_review_loop(dev, review, fix, max_iterations=5)
    assert result.passed is False
    assert result.escalated is True          # 같은 finding 3회 → no-progress escalation
    assert result.iterations == 3
    assert len(trace.events(event_type="LoopEvent")) == 3


async def test_review_loop_passes(tmp_path):
    orch, _, _ = make_orch(tmp_path)
    dev = await orch.spawn(Role.DEVELOPER, "DEFAULT", execution_id="E1",
                           node_id="n1", task_scope="*")
    calls = {"n": 0}

    async def review(_):
        calls["n"] += 1
        return "PASS" if calls["n"] >= 2 else "NOT_PASS: missing test"
    async def fix(_): pass

    result = await orch.run_review_loop(dev, review, fix)
    assert result.passed is True and result.iterations == 2


def test_review_queue_scale_signal():
    q = ReviewQueue(max_reviewers=3)
    assert q.scale_signal() == 1                  # 기본 1 유지 (§9.1)
    for i in range(7):
        q.submit(f"dev-{i}")
    assert q.scale_signal() == 3                  # 압력 상승 → max까지
