"""§17 설정 로딩 — 설정이 정본, 코드 폴백 없음."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .schema import EffortLevel, Provider, Role

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "harness.yaml"

REQUIRED_ROLE_DEFAULTS = frozenset(Role)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class TierSpec:
    provider: Provider
    model: str
    default_effort: EffortLevel


@dataclass(frozen=True)
class RoleDefault:
    tier: str
    effort: EffortLevel | None = None


@dataclass(frozen=True)
class LoopPolicy:
    max_iterations: int
    max_duration_minutes: int
    max_token_budget: int          # 실행 전체 hard cap
    same_finding_escalation_threshold: int
    # role별 누적 토큰 상한 (role 이름 -> 토큰). 비어 있으면 role 가드 없음.
    role_budgets: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class HarnessConfig:
    tiers: dict[str, TierSpec]
    role_defaults: dict[Role, RoleDefault]
    loop_policy: LoopPolicy


def load(path: str | Path | None = None) -> HarnessConfig:
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        raise ConfigError(f"config not found: {p}")
    raw = yaml.safe_load(p.read_text())
    raw_role_defaults = raw.get("roleDefaults")
    if not raw_role_defaults:
        raise ConfigError(f"config has no roleDefaults: {p}")
    try:
        tiers = {
            name: TierSpec(Provider(t["provider"]), t["model"], EffortLevel(t["effort"]))
            for name, t in (raw.get("tiers") or {}).items()
        }
        role_defaults = {
            Role(name): RoleDefault(d["tier"],
                                    EffortLevel(d["effort"]) if d.get("effort") else None)
            for name, d in raw_role_defaults.items()
        }
    except (KeyError, ValueError) as e:
        raise ConfigError(f"invalid config {p}: {e}") from e
    if not tiers:
        raise ConfigError(f"config has no tiers: {p}")
    unknown_tiers = {rd.tier for rd in role_defaults.values()} - set(tiers)
    if unknown_tiers:
        raise ConfigError(
            f"roleDefaults reference unknown tiers {sorted(unknown_tiers)}: {p}")
    missing_roles = REQUIRED_ROLE_DEFAULTS - set(role_defaults)
    if missing_roles:
        raise ConfigError(
            f"roleDefaults missing required roles "
            f"{sorted(r.value for r in missing_roles)}: {p}")
    raw_lp = raw.get("loopPolicy")
    if not raw_lp:
        raise ConfigError(f"config has no loopPolicy: {p}")
    role_budgets: dict[str, int] = {}
    for name, v in (raw_lp.get("roleBudgets") or {}).items():
        try:
            role = Role(name)
            budget = int(v)
        except ValueError as e:
            raise ConfigError(f"invalid roleBudgets entry {name!r} in {p}: {e}") from e
        if budget <= 0:
            raise ConfigError(f"roleBudgets[{name}] must be positive: {p}")
        role_budgets[role.value] = budget
    try:
        lp = LoopPolicy(int(raw_lp["maxIterations"]), int(raw_lp["maxDurationMinutes"]),
                        int(raw_lp["maxTokenBudget"]),
                        int(raw_lp["sameFindingEscalationThreshold"]),
                        role_budgets=role_budgets)
    except (KeyError, ValueError, TypeError) as e:
        raise ConfigError(f"invalid loopPolicy {p}: {e}") from e
    if min(lp.max_iterations, lp.max_duration_minutes, lp.max_token_budget,
           lp.same_finding_escalation_threshold) <= 0:
        raise ConfigError(f"loopPolicy values must be positive: {p}")
    return HarnessConfig(tiers=tiers, role_defaults=role_defaults, loop_policy=lp)
