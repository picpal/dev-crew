import pytest
from devcrew.roles import RoleBundleError, load_bundle, missing_required_keys
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


def test_missing_required_keys_recurses_into_array_items():
    """wave 2 F2 회귀: codex 재리뷰가 재현한 그대로 — findings가 array이고 그 items가
    object schema일 때, wave 1의 missing_required_keys()는 object property만
    재귀해 배열 원소 내부의 required 키 누락(예: `findings: [{}]`)을 그냥
    통과시켰다(빈 리스트 반환). 이제는 items schema로도 재귀해 잡아낸다."""
    schema = load_bundle(Role.REVIEWER).schema
    structured = {"status": "PASS", "summary": "ok", "verdict": "PASS", "findings": [{}]}
    missing = missing_required_keys(schema, structured)
    assert missing              # wave 1에서는 [] (빈 리스트)였다 — 이제는 비어있지 않다
    assert all(m.startswith("findings[0].") for m in missing)
    assert {"severity", "file", "line", "description"} == {
        m.split(".", 1)[1] for m in missing}


def test_missing_required_keys_array_items_with_all_fields_present_is_clean():
    schema = load_bundle(Role.REVIEWER).schema
    structured = {"status": "PASS", "summary": "ok", "verdict": "PASS",
                 "findings": [{"severity": "minor", "file": "a.py", "line": 1,
                              "description": "d"}]}
    assert missing_required_keys(schema, structured) == []


# ── TUTOR / TUTOR_VERIFIER ──────────────────────────────────────────────────
@pytest.mark.parametrize("role", [Role.TUTOR, Role.TUTOR_VERIFIER])
def test_tutor_bundles_load_with_common_skeleton(role):
    b = load_bundle(role)
    assert len(b.prompt) > 200
    assert b.schema["properties"]["status"]["enum"] == STATUS_ENUM
    assert set(b.schema["required"]) >= {"status", "summary"}


def test_tutor_question_schema_is_strict_and_carries_evidence():
    """provider strict 모드 요구 — 전 필드 required, additionalProperties false."""
    q = load_bundle(Role.TUTOR).schema["properties"]["questions"]["items"]
    assert q["additionalProperties"] is False
    assert set(q["required"]) == set(q["properties"])
    for key in ("area", "type", "stem", "options", "answer_index",
                "evidence", "explanation", "diagram"):
        assert key in q["properties"]
    ev = q["properties"]["evidence"]["items"]
    assert set(ev["required"]) == {"path", "start_line", "end_line", "quote"}


def test_verifier_schema_reports_per_question_verdicts():
    v = load_bundle(Role.TUTOR_VERIFIER).schema["properties"]["verdicts"]["items"]
    assert v["properties"]["verdict"]["enum"] == ["PASS", "REJECT"]
    assert set(v["required"]) == {"index", "verdict", "reason"}
