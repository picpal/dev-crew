import pytest
from devcrew.roles import RoleBundleError, load_bundle
from devcrew.schema import Role

WORKERS = [Role.EXPLORER, Role.DEVELOPER, Role.REVIEWER, Role.QA]
STATUS_ENUM = ["PASS", "NOT_PASS", "NEED_REPLAN", "BLOCKED", "INSUFFICIENT_CAPABILITY"]


@pytest.mark.parametrize("role", WORKERS)
def test_bundle_loads_with_common_skeleton(role):
    b = load_bundle(role)
    assert len(b.prompt) > 200            # 실제 지침이 있어야 함
    props = b.schema["properties"]
    assert props["status"]["enum"] == STATUS_ENUM
    assert "summary" in props
    assert set(b.schema["required"]) >= {"status", "summary"}
    assert len(b.version) == 12


def test_role_specific_fields():
    assert "findings" in load_bundle(Role.EXPLORER).schema["properties"]
    assert "changed_files" in load_bundle(Role.DEVELOPER).schema["properties"]
    assert "verdict" in load_bundle(Role.REVIEWER).schema["properties"]
    assert "results" in load_bundle(Role.QA).schema["properties"]


def test_missing_bundle_fail_fast():
    with pytest.raises(RoleBundleError):
        load_bundle(Role.ORCHESTRATOR)    # 이번 effort 범위 밖 — 번들 없음
