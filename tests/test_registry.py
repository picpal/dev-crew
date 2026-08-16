from devcrew.schema import InstanceStatus
from devcrew.store.registry import SessionRegistry
from tests.test_trace import make_inst


def test_active_only_lifecycle(tmp_path):
    reg = SessionRegistry(tmp_path / "harness.db")
    inst = make_inst("DEV-001", InstanceStatus.RUNNING)
    reg.upsert(inst, provider_ref="/fake/session.jsonl")
    assert [r["instance_id"] for r in reg.active()] == ["DEV-001"]
    reg.finish("DEV-001")          # 종결 → 행 제거 (#15)
    assert reg.active() == []


def test_checkpoint_feeds_handoff(tmp_path):
    reg = SessionRegistry(tmp_path / "harness.db")
    reg.upsert(make_inst("DEV-002", InstanceStatus.RUNNING), provider_ref="ref")
    reg.checkpoint("DEV-002", summary="API 골격 작성 완료", artifacts=["src/api.py"])
    h = reg.handoff("DEV-002")
    assert h["task_scope"] == "*"
    assert h["last_summary"] == "API 골격 작성 완료"
    assert h["artifacts"] == ["src/api.py"]
    assert h["reason"] is None      # escalation/recovery가 채운다


def test_lazy_verify_marks_resumable_or_failed(tmp_path):
    reg = SessionRegistry(tmp_path / "harness.db")
    reg.upsert(make_inst("A", InstanceStatus.RUNNING), provider_ref="exists")
    reg.upsert(make_inst("B", InstanceStatus.RUNNING), provider_ref="missing")
    result = reg.lazy_verify({"CLAUDE_CODE": lambda row: row["provider_ref"] == "exists"})
    assert result == {"A": "RESUMABLE", "B": "FAILED_RECOVERY"}
