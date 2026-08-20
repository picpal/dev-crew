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
class LeaderContext:
    """crew leader(ORCHESTRATOR) 결정 세션의 컨텍스트 정책.

    persistent=False면 결정마다 fresh 세션(종전 동작). True면 스레드 단위로 세션을
    유지해 이전 결정 맥락을 들고 판단하고, 컨텍스트 점유가 window*ratio에 닿으면
    leader가 스스로 요약해 새 세션에 인계한다(compaction).
    """
    persistent: bool = False
    window_tokens: int = 1_000_000
    compact_at_ratio: float = 0.5

    @property
    def compact_at(self) -> int:
        return int(self.window_tokens * self.compact_at_ratio)


@dataclass(frozen=True)
class HarnessConfig:
    tiers: dict[str, TierSpec]
    role_defaults: dict[Role, RoleDefault]
    loop_policy: LoopPolicy
    leader_context: LeaderContext = field(default_factory=LeaderContext)


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
    raw_lc = raw.get("leaderContext") or {}
    try:
        lc = LeaderContext(
            persistent=bool(raw_lc.get("persistent", False)),
            window_tokens=int(raw_lc.get("windowTokens", 1_000_000)),
            compact_at_ratio=float(raw_lc.get("compactAtRatio", 0.5)))
    except (ValueError, TypeError) as e:
        raise ConfigError(f"invalid leaderContext {p}: {e}") from e
    if lc.window_tokens <= 0 or not (0 < lc.compact_at_ratio < 1):
        raise ConfigError(
            f"leaderContext: windowTokens>0, 0<compactAtRatio<1 이어야 한다: {p}")
    return HarnessConfig(tiers=tiers, role_defaults=role_defaults, loop_policy=lp,
                         leader_context=lc)
