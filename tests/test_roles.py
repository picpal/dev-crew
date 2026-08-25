import pytest
from devcrew.roles import RoleBundleError, load_bundle, missing_required_keys
from devcrew.orchestrator import BUNDLED_ROLES
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
    # 스키마와 workflow.DECISION_ACTIONS는 같은 목록이어야 한다 — 한쪽만 늘리면
    # 엔진이 허용하는 action을 모델이 낼 수 없거나(스키마 거절), 그 반대가 된다.
    from devcrew.workflow import ALLOWED_BY_TRIGGER, DECISION_ACTIONS
    assert d["action"]["enum"] == DECISION_ACTIONS
    for trigger, actions in ALLOWED_BY_TRIGGER.items():
        assert set(actions) <= set(DECISION_ACTIONS), trigger
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


def test_long_text_field_is_last_property():
    """긴 자유 텍스트 property는 스키마의 **마지막**이어야 한다 (2026-08-24).

    모델은 값이 대략 1,300자를 넘으면 그 property를 XML 파라미터 블록으로 방출하는데,
    닫는 태그를 `</parameter>`가 아니라 `</answer>`처럼 **필드 이름으로 잘못 쓴다**.
    파서는 거기서 값을 닫지 않고 뒤따르는 파라미터를 통째로 그 문자열 안으로 흡수한다.
    그래서 뒤에 선언된 property가 객체에서 사라지고, `required`(불변조건 5)를 위반해
    CLI가 재제출을 요구한다. 3회 실패하면 모델은 스키마만 통과할 최소 payload
    (`answer: "test"`)를 낸다 — 하네스는 거절된 시도를 볼 수 없으므로 그 쓰레기를
    정상 답변으로 받아 학습자에게 그대로 보여줬다.

    긴 필드를 마지막에 두면 흡수될 뒤 필드가 없어 이 경로가 사라진다. 다른 role은
    전부 우연히 그 배치였고 `tutor_ta`만 아니었다 — 그래서 여기서만 재현됐다.
    """
    from devcrew.roles import load_bundle
    from devcrew.schema import Role

    # role → 그 role에서 가장 길어지는 property
    # 어느 필드가 길어지는지는 **실측으로** 정한다 (2026-08-24 trace):
    # EXPLORER.findings 2008자 / DEVELOPER.tests 471자(changed_files는 14자라
    # array라고 긴 것이 아니다) / TUTOR_TA.answer 1992자.
    longest = {Role.TUTOR_TA: "answer", Role.EXPLORER: "findings",
               Role.DEVELOPER: "tests", Role.REVIEWER: "findings",
               Role.QA: "results", Role.TUTOR: "questions",
               Role.TUTOR_VERIFIER: "verdicts", Role.ORCHESTRATOR: "report"}
    for role, field in longest.items():
        props = list(load_bundle(role).schema["properties"])
        assert props[-1] == field, (
            f"{role.value}: 긴 필드 {field!r}가 마지막이 아니다 — 뒤의 "
            f"{props[props.index(field) + 1:]}가 흡수될 수 있다")


def test_brain_scalars_precede_arrays():
    """BRAIN은 긴 배열이 여러 개라 "긴 것을 마지막에"로 다 못 막는다 — 대신 짧은
    스칼라를 배열 **앞**에 둔다. 특히 `target_repo`는 brief의 대상 repo를 정하는
    값인데 긴 배열 셋 뒤에 있었다: 흡수되면 brief가 대상을 잃는다.
    같은 결함의 배경은 [test_long_text_field_is_last_property]에 적혀 있다.
    """
    from devcrew.roles import load_bundle
    from devcrew.schema import Role

    props = load_bundle(Role.BRAIN).schema["properties"]
    kinds = [(k, v.get("type")) for k, v in props.items()]
    first_array = next(i for i, (_, t) in enumerate(kinds) if t == "array")
    after = [k for k, t in kinds[first_array:] if t != "array"]
    assert not after, f"배열 뒤에 스칼라가 있다: {after}"


@pytest.mark.parametrize("role", sorted(BUNDLED_ROLES, key=lambda r: r.value))
def test_every_bundled_role_actually_has_a_bundle(role):
    """`BUNDLED_ROLES`에 든 role은 **전부** 디스크에 번들이 있어야 한다.

    이 테스트가 없어서, 지금까지는 role을 enum·정책·config에 등록하고 `roles/<name>/`을
    만들지 않아도 전 테스트가 초록이었다 — 실패는 프로덕션에서 첫 spawn 때 났다
    (lessons C10의 원래 사례가 그것이다). 목록을 하드코딩한 파라미터로 돌면 새 role이
    자동으로 빠지므로, **BUNDLED_ROLES 자체를** 돌린다.
    """
    from devcrew.roles import load_bundle

    b = load_bundle(role)
    assert len(b.prompt) > 200, f"{role.value}: 프롬프트가 비어 있다"
    props = b.schema["properties"]
    assert props["status"]["enum"] == STATUS_ENUM
    assert set(b.schema["required"]) == set(props), \
        f"{role.value}: required가 모든 property를 담지 않았다 (불변조건 5)"
    _assert_strict(b.schema)


def test_only_declared_roles_get_skills():
    """`skills`를 선언한 role만 Skill 도구를 갖는다.

    SDK는 `skills=[이름]`을 받으면 그 스킬만 호출 가능하게 열어 준다 — 전역 스킬 전체를
    여는 `"all"`은 쓰지 않는다(컨텍스트 낭비 + 의도치 않은 능력). `setting_sources`도
    넘기지 않는다: 없이도 Skill 호출이 되는 것을 실측했고, 넣으면 사용자 전역 설정이
    워커에 통째로 딸려 온다.
    """
    from devcrew.enforcement import ROLE_POLICY, claude_options_kwargs
    from devcrew.schema import Role

    assert claude_options_kwargs(Role.TUTOR_VIS, cwd="/tmp/x")["skills"] == ["vision"]
    for role in (Role.TUTOR, Role.TUTOR_TA, Role.TUTOR_CODE, Role.DEVELOPER):
        assert "skills" not in claude_options_kwargs(role, cwd="/tmp/x"), role.value
        assert not ROLE_POLICY[role].skills, role.value


def test_tutor_vis_can_write_but_never_into_a_repo():
    """그리기는 파일을 만들어야 한다 — 그래서 쓰기를 준다.

    대신 **cwd가 사용자 repo가 아니다**(`tutor_vis`가 매번 임시 디렉토리를 만든다).
    다른 tutor role은 repo를 cwd로 받으므로 쓰기를 주면 사용자 저장소가 더러워진다 —
    그 둘을 한 role에 섞지 않는 것이 이 분리의 이유다.
    """
    from devcrew.enforcement import ROLE_POLICY
    from devcrew.schema import Role

    p = ROLE_POLICY[Role.TUTOR_VIS]
    assert "Bash" in p.allowed_tools
    # 쓰기는 **경로 게이트를 타야** 한다 — allowed_tools에 있으면 SDK가 콜백 이전에
    # 자동 승인해 임시 디렉토리 밖으로 나가는 것을 막을 수 없다.
    assert p.scoped_write_tools == ["Write", "Edit"]
    assert "Write" not in p.allowed_tools and "Edit" not in p.allowed_tools
    for role in (Role.TUTOR, Role.TUTOR_TA, Role.TUTOR_CODE):
        pol = ROLE_POLICY[role]
        assert not pol.scoped_write_tools
        assert "Write" not in pol.allowed_tools
        assert "Bash" not in pol.allowed_tools
