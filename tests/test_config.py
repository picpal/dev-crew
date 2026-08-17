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
        "roleDefaults: {}\n")
    with pytest.raises(ConfigError):
        load(bad)


def test_routing_consumes_yaml():
    from devcrew.routing import TIERS, resolve
    assert TIERS["DEFAULT"].model == "claude-sonnet-5"
    assert resolve("CHEAP").effort == "low"
