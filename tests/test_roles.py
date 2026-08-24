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
                "evidence", "explanation", "diagram", "source_key"):
        assert key in q["properties"]
    ev = q["properties"]["evidence"]["items"]
    assert set(ev["required"]) == {"path", "start_line", "end_line", "quote"}


def test_verifier_schema_reports_per_question_verdicts():
    v = load_bundle(Role.TUTOR_VERIFIER).schema["properties"]["verdicts"]["items"]
    assert v["properties"]["verdict"]["enum"] == ["PASS", "REJECT"]
    assert set(v["required"]) == {"index", "verdict", "reason"}


def test_tutor_ta_bundle_loads_with_answer_and_citations():
    """후속 질문 답변 role — 번들이 공통 골격을 지키고 답변 필드를 갖는다."""
    from devcrew.roles import load_bundle
    from devcrew.schema import Role

    b = load_bundle(Role.TUTOR_TA)
    props = b.schema["properties"]
    assert "answer" in props and props["answer"]["type"] == "string"
    cit = props["citations"]["items"]
    assert cit["required"] == ["path", "start_line", "end_line", "quote"]
    assert cit["additionalProperties"] is False
    assert "근거" in b.prompt        # 근거 없는 답변 금지가 프롬프트에 있다


def test_tutor_ta_is_read_only():
    """학습 도구가 코드를 만질 이유가 없다 — TUTOR와 같은 격리."""
    from devcrew.enforcement import ROLE_POLICY
    from devcrew.schema import Role

    p = ROLE_POLICY[Role.TUTOR_TA]
    assert not p.scoped_write_tools
    assert not any(t.startswith("Bash") for t in p.allowed_tools)


def test_tutor_ta_has_tier_and_budget():
    """roleDefaults·roleBudgets를 빠뜨리면 엔진이 뜨지 않는다 (lessons C10)."""
    from devcrew.config import load
    from devcrew.schema import Role

    cfg = load()
    assert cfg.role_defaults[Role.TUTOR_TA].tier == "DEFAULT"
    assert cfg.loop_policy.role_budgets["TUTOR_TA"] > 0


def test_every_bundle_dir_on_disk_is_in_orchestrator_bundled_roles():
    """roles/<name>/ 디렉토리를 만들고 BUNDLED_ROLES에 등록하는 걸 잊으면
    spawn()이 role_bundle_version을 안 찍고, start_worker()가 버전 불일치로
    RoleBundleError를 던진다 (orchestrator.py spawn/start_worker) — role을
    하나 추가할 때마다 이 테이블도 손으로 갱신해야 하므로, 하드코딩된 role
    목록이 아니라 실제 디스크의 bundle 디렉토리를 기준으로 검사한다."""
    from devcrew.orchestrator import BUNDLED_ROLES
    from devcrew.roles import ROLES_DIR
    from devcrew.schema import Role

    by_dir_name = {r.value.lower(): r for r in Role}
    dirs_on_disk = {p.name for p in ROLES_DIR.iterdir() if p.is_dir()}
    roles_with_bundles = {by_dir_name[name] for name in dirs_on_disk if name in by_dir_name}
    missing = roles_with_bundles - BUNDLED_ROLES
    assert not missing, f"roles/ 번들은 있는데 BUNDLED_ROLES에 없는 role: {missing}"
