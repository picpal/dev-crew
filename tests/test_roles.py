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


def test_orchestrator_bundle_loads():
    b = load_bundle(Role.ORCHESTRATOR)
    props = b.schema["properties"]
    d = props["decision"]["properties"]
    assert d["action"]["enum"] == ["PROCEED", "RETRY_NODE", "ESCALATE_MODEL",
                                  "SKIP_NODE", "REPLAN", "ASK_USER", "ABORT"]
    assert d["target_node"]["type"] == ["string", "null"]
    _assert_strict(b.schema)


def _assert_strict(obj):
    """Verify object is OpenAI strict-compatible: required == all properties."""
    if obj.get("type") == "object" or (isinstance(obj.get("type"), list) and "object" in obj["type"]):
        props = obj.get("properties") or {}
        required = set(obj.get("required") or [])
        assert required == set(props), f"required {sorted(required)} != all keys {sorted(props)}"
    # Recurse into nested objects
    for v in (obj.get("properties") or {}).values():
        _assert_strict(v)
    # Handle array items
    if obj.get("items"):
        _assert_strict(obj["items"])


@pytest.mark.parametrize("role", WORKERS)
def test_schema_is_openai_strict_compatible(role):
    """Regression guard: role schemas must be OpenAI strict-compatible."""
    bundle = load_bundle(role)
    _assert_strict(bundle.schema)
