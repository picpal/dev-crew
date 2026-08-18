import pytest
from devcrew.adapters.base import FakeAdapter, TurnOutcome
from devcrew.orchestrator import Orchestrator, ReviewQueue
from devcrew.roles import RoleBundleError
from devcrew.schema import EffortLevel, Provider, Role, Usage
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


def test_review_queue_scale_signal():
    q = ReviewQueue(max_reviewers=3)
    assert q.scale_signal() == 1                  # 기본 1 유지 (§9.1)
    for i in range(7):
        q.submit(f"dev-{i}")
    assert q.scale_signal() == 3                  # 압력 상승 → max까지


async def test_spawn_records_bundle_version(tmp_path):
    from devcrew.roles import load_bundle
    orch, trace, reg = make_orch(tmp_path)
    inst = await orch.spawn(Role.EXPLORER, "CHEAP", execution_id="E1",
                            node_id="n1", task_scope="*")
    assert inst.role_bundle_version == load_bundle(Role.EXPLORER).version


async def test_start_worker_injects_bundle(tmp_path):
    orch, _, _ = make_orch(tmp_path, FakeAdapter(structured_script=[{"status": "PASS", "summary": "ok"}]))
    fake = orch.adapters[Provider.CLAUDE_CODE]
    inst = await orch.spawn(Role.QA, "CHEAP", execution_id="E1",
                            node_id="n1", task_scope="*")
    await orch.start_worker(inst, "검증 시작")
    assert fake.last_system_prompt.startswith("# QA")
    assert fake.last_output_schema["properties"]["results"]


async def test_spawn_without_bundle_for_out_of_scope_role(tmp_path):
    orch, _, _ = make_orch(tmp_path)
    inst = await orch.spawn(Role.ARCHITECT, "DEFAULT", execution_id="E1",
                            node_id="n1", task_scope="*")
    assert inst.role_bundle_version is None


async def test_start_worker_injects_task_scope(tmp_path):
    orch, _, _ = make_orch(tmp_path, FakeAdapter(structured_script=[{"status": "PASS", "summary": "ok"}]))
    fake = orch.adapters[Provider.CLAUDE_CODE]
    inst = await orch.spawn(Role.QA, "CHEAP", execution_id="E1",
                            node_id="n1", task_scope="파일 X만 수정")
    await orch.start_worker(inst, "검증 시작")
    assert fake.last_system_prompt.endswith("파일 X만 수정")


async def test_start_worker_rejects_bundle_version_drift(tmp_path):
    orch, _, _ = make_orch(tmp_path)
    inst = await orch.spawn(Role.QA, "CHEAP", execution_id="E1",
                            node_id="n1", task_scope="*")
    inst.role_bundle_version = "tampered"     # bundle 변경/조작 시뮬레이션 (finding #5)
    with pytest.raises(RoleBundleError):
        await orch.start_worker(inst, "검증 시작")


async def test_consume_result_valid_explorer_pass(tmp_path):
    orch, trace, _ = make_orch(tmp_path)
    inst = await orch.spawn(Role.EXPLORER, "CHEAP", execution_id="E1",
                            node_id="n1", task_scope="*")
    # EXPLORER output.schema.json required: status, summary, findings, affected_files
    # (finding #2 — consume_result가 이제 role bundle schema 전체를 검증한다)
    structured = {"status": "PASS", "summary": "ok", "findings": [], "affected_files": []}
    outcome = TurnOutcome(text="", usage=Usage(), structured=structured)
    result = orch.consume_result(inst, outcome)
    assert result == "PASS"
    evs = trace.events(event_type="WorkerResultEvent")
    assert evs[0]["payload"] == {"role": "EXPLORER", "status": "PASS", "structured": structured}


async def test_consume_result_reviewer_uses_verdict_not_status(tmp_path):
    orch, trace, _ = make_orch(tmp_path)
    inst = await orch.spawn(Role.REVIEWER, "CODEX_DEFAULT", execution_id="E1",
                            node_id="n1", task_scope="*")
    # status=PASS(검토를 마쳤다)이어도 verdict=NOT_PASS(코드가 실패)면 전이는 NOT_PASS.
    outcome = TurnOutcome(text="", usage=Usage(),
                          structured={"status": "PASS", "summary": "ok",
                                      "verdict": "NOT_PASS", "findings": []})
    result = orch.consume_result(inst, outcome)
    assert result == "NOT_PASS"
    evs = trace.events(event_type="WorkerResultEvent")
    assert evs[0]["payload"] == {"role": "REVIEWER", "status": "PASS", "verdict": "NOT_PASS",
                                 "structured": outcome.structured}


async def test_consume_result_malformed_structured_logs_event(tmp_path):
    orch, trace, _ = make_orch(tmp_path)
    inst = await orch.spawn(Role.EXPLORER, "CHEAP", execution_id="E1",
                            node_id="n1", task_scope="*")
    outcome = TurnOutcome(text="", usage=Usage(), structured=None)
    result = orch.consume_result(inst, outcome)
    assert result == "NEED_REPLAN"
    evs = trace.events(event_type="MalformedResultEvent")
    assert len(evs) == 1
    assert evs[0]["payload"]["role"] == "EXPLORER"


async def test_consume_result_valid_status_but_missing_required_fields_is_malformed(tmp_path):
    """finding #2 회귀: codex 리뷰가 재현한 그대로 — status만 유효(PASS)하고 role bundle
    schema의 나머지 required(summary/changed_files/build/tests)가 전부 빠진 DEVELOPER
    출력은 더 이상 그대로 PASS 처리되지 않는다. malformed로 강등돼 NEED_REPLAN이 된다."""
    orch, trace, _ = make_orch(tmp_path)
    inst = await orch.spawn(Role.DEVELOPER, "DEFAULT", execution_id="E1",
                            node_id="n1", task_scope="*")
    outcome = TurnOutcome(text="", usage=Usage(), structured={"status": "PASS"})
    result = orch.consume_result(inst, outcome)
    assert result == "NEED_REPLAN"
    evs = trace.events(event_type="MalformedResultEvent")
    assert len(evs) == 1
    assert evs[0]["payload"]["role"] == "DEVELOPER"


async def test_consume_result_reviewer_blocked_status_not_overridden_by_verdict(tmp_path):
    """재리뷰 신규 finding: status=BLOCKED(검토 불가)면 verdict=PASS라도 BLOCKED를
    그대로 전파해야 한다 — verdict는 status가 PASS(검토를 실제로 마쳤을 때)일 때만
    쓴다."""
    orch, trace, _ = make_orch(tmp_path)
    inst = await orch.spawn(Role.REVIEWER, "CODEX_DEFAULT", execution_id="E1",
                            node_id="n1", task_scope="*")
    outcome = TurnOutcome(text="", usage=Usage(),
                          structured={"status": "BLOCKED", "summary": "diff를 읽을 수 없음",
                                      "verdict": "PASS", "findings": []})
    result = orch.consume_result(inst, outcome)
    assert result == "BLOCKED"
    evs = trace.events(event_type="WorkerResultEvent")
    assert evs[0]["payload"] == {"role": "REVIEWER", "status": "BLOCKED",
                                 "structured": outcome.structured}


async def test_consume_result_as_role_override(tmp_path):
    """W3: as_role로 판정 role을 inst.role과 다르게 지정할 수 있다 — WorkerResultEvent
    payload도 판정에 쓰인 role(as_role)을 기록한다."""
    orch, trace, _ = make_orch(tmp_path)
    dev = await orch.spawn(Role.DEVELOPER, "DEFAULT", execution_id="E1",
                           node_id="n1", task_scope="*")
    outcome = TurnOutcome(text="", usage=Usage(),
                          structured={"status": "PASS", "summary": "ok",
                                      "verdict": "NOT_PASS", "findings": []})
    result = orch.consume_result(dev, outcome, as_role=Role.REVIEWER)
    assert result == "NOT_PASS"
    evs = trace.events(event_type="WorkerResultEvent")
    assert evs[0]["payload"] == {"role": "REVIEWER", "status": "PASS", "verdict": "NOT_PASS",
                                 "structured": outcome.structured}
