import pytest
from devcrew.routing import TIERS, Resolved, RoutingValidationError, resolve
from devcrew.schema import EffortLevel, Provider


def test_tier_table_is_pinned_full_ids():
    assert TIERS["CHEAP"].model == "claude-sonnet-5"
    assert TIERS["DEFAULT"].model == "claude-sonnet-5"
    assert TIERS["HIGH_CAPABILITY"].model == "claude-opus-5"
    assert TIERS["CODEX_DEFAULT"].model == "gpt-5.6-terra"
    assert TIERS["CODEX_HIGH_REASONING"].model == "gpt-5.6-sol"


def test_resolve_default_effort():
    r = resolve("CHEAP")
    assert r == Resolved(Provider.CLAUDE_CODE, "claude-sonnet-5", "low")
    r = resolve("CODEX_HIGH_REASONING", EffortLevel.XHIGH)  # routing 승격
    assert r.effort == "xhigh"


def test_resolve_rejects_unknown_tier():
    with pytest.raises(RoutingValidationError):
        resolve("NOPE")


def test_unsupported_model_effort_combo_is_error_not_downgrade():
    # 지원 매트릭스에 없는 모델은 어떤 effort와도 조합 불가 (G2 사례 재현)
    from devcrew.routing import validate_combo
    with pytest.raises(RoutingValidationError):
        validate_combo("claude-haiku-4-5", "low")
    # Claude에 없는 값
    with pytest.raises(RoutingValidationError):
        validate_combo("claude-sonnet-5", "none")
    # 정상 조합
    validate_combo("gpt-5.6-terra", "none")
    validate_combo("claude-opus-5", "max")
