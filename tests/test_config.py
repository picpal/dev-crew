import pytest
from devcrew.config import ConfigError, load
from devcrew.schema import EffortLevel, Role


def test_load_default_config():
    cfg = load()   # config/harness.yaml
    assert cfg.tiers["CHEAP"].model == "claude-sonnet-5"
    assert cfg.tiers["CODEX_HIGH_REASONING"].model == "gpt-5.6-sol"
    assert cfg.role_defaults[Role.EXPLORER].tier == "CHEAP"
    assert cfg.role_defaults[Role.REVIEWER].tier == "CODEX_DEFAULT"


def test_missing_config_is_fail_fast(tmp_path):
    with pytest.raises(ConfigError):
        load(tmp_path / "nope.yaml")


def test_invalid_effort_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "tiers:\n  CHEAP: {provider: CLAUDE_CODE, model: claude-sonnet-5, effort: TURBO}\n"
        "roleDefaults:\n  EXPLORER: {tier: CHEAP}\n")
    with pytest.raises(ConfigError):
        load(bad)


def test_missing_role_defaults_section_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "tiers:\n  CHEAP: {provider: CLAUDE_CODE, model: claude-sonnet-5, effort: LOW}\n")
    with pytest.raises(ConfigError):
        load(bad)


def test_empty_role_defaults_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "tiers:\n  CHEAP: {provider: CLAUDE_CODE, model: claude-sonnet-5, effort: LOW}\n"
        "roleDefaults: {}\n")
    with pytest.raises(ConfigError):
        load(bad)


def test_role_default_unknown_tier_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "tiers:\n  CHEAP: {provider: CLAUDE_CODE, model: claude-sonnet-5, effort: LOW}\n"
        "roleDefaults:\n  EXPLORER: {tier: NONEXISTENT}\n")
    with pytest.raises(ConfigError):
        load(bad)


def test_role_defaults_incomplete_rejected(tmp_path):
    """W2-2: EXPLORER 하나만 있는 roleDefaults는 나머지 필수 role이 빠졌으므로 거부."""
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "tiers:\n  CHEAP: {provider: CLAUDE_CODE, model: claude-sonnet-5, effort: LOW}\n"
        "roleDefaults:\n  EXPLORER: {tier: CHEAP}\n")
    with pytest.raises(ConfigError):
        load(bad)


def test_routing_consumes_yaml():
    from devcrew.routing import TIERS, resolve
    assert TIERS["DEFAULT"].model == "claude-sonnet-5"
    assert resolve("CHEAP").effort == "low"


# Task 1 tests: loopPolicy + ORCHESTRATOR roleDefaults
def test_loop_policy_loaded():
    cfg = load()
    assert cfg.loop_policy.max_iterations == 5
    assert cfg.loop_policy.max_duration_minutes == 60
    assert cfg.loop_policy.max_token_budget == 300000
    assert cfg.loop_policy.same_finding_escalation_threshold == 3


def test_loop_policy_missing_fails(tmp_path):
    p = tmp_path / "h.yaml"
    p.write_text(
        "tiers:\n  CHEAP: {provider: CLAUDE_CODE, model: claude-sonnet-5, effort: LOW}\n"
        "roleDefaults:\n"
        "  ORCHESTRATOR: {tier: CHEAP}\n"
        "  EXPLORER: {tier: CHEAP}\n"
        "  ARCHITECT: {tier: CHEAP}\n"
        "  DEVELOPER: {tier: CHEAP}\n"
        "  SECURITY: {tier: CHEAP}\n"
        "  QA: {tier: CHEAP}\n"
        "  REVIEWER: {tier: CHEAP}\n"
            "  BRAIN: {tier: CHEAP}\n")
    with pytest.raises(ConfigError, match="loopPolicy"):
        load(p)


def test_loop_policy_nonpositive_fails(tmp_path):
    p = tmp_path / "h.yaml"
    p.write_text(
        "tiers:\n  CHEAP: {provider: CLAUDE_CODE, model: claude-sonnet-5, effort: LOW}\n"
        "roleDefaults:\n"
        "  ORCHESTRATOR: {tier: CHEAP}\n"
        "  EXPLORER: {tier: CHEAP}\n"
        "  ARCHITECT: {tier: CHEAP}\n"
        "  DEVELOPER: {tier: CHEAP}\n"
        "  SECURITY: {tier: CHEAP}\n"
        "  QA: {tier: CHEAP}\n"
        "  REVIEWER: {tier: CHEAP}\n"
            "  BRAIN: {tier: CHEAP}\n"
        "loopPolicy:\n"
        "  maxIterations: 0\n"
        "  maxDurationMinutes: 60\n"
        "  maxTokenBudget: 300000\n"
        "  sameFindingEscalationThreshold: 3\n")
    with pytest.raises(ConfigError, match="positive"):
        load(p)


def test_orchestrator_role_default_required():
    cfg = load()
    assert cfg.role_defaults[Role.ORCHESTRATOR].tier == "HIGH_CAPABILITY"
