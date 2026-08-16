"""Model routing — tier 매핑(#12)과 (model, effort) 사전 검증(§6.1).

Claude Code는 미지원 effort를 조용히 하향하므로(model-effort.md G3/G6)
provider 호출 전에 이 매트릭스로 검증한다. 매트릭스가 유일한 진실이다.
"""
from __future__ import annotations

from dataclasses import dataclass

from .schema import EffortLevel, Provider


class RoutingValidationError(Exception):
    pass


# 조사 근거: docs/research/model-effort.md §1.2, §2.2 (2026-08-14 검증)
SUPPORT_MATRIX: dict[str, frozenset[str]] = {
    "claude-sonnet-5": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "claude-opus-5": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "gpt-5.6-terra": frozenset({"none", "low", "medium", "high", "xhigh", "max"}),
    "gpt-5.6-sol": frozenset({"none", "low", "medium", "high", "xhigh", "max"}),
}

_EFFORT_STR = {
    EffortLevel.LOW: "low",
    EffortLevel.MEDIUM: "medium",
    EffortLevel.HIGH: "high",
    EffortLevel.XHIGH: "xhigh",
    EffortLevel.MAX: "max",
}


@dataclass(frozen=True)
class Tier:
    provider: Provider
    model: str
    default_effort: EffortLevel


@dataclass(frozen=True)
class Resolved:
    provider: Provider
    model: str
    effort: str


# DESIGN.md §17 — tier = (full model ID, 기본 effort) 쌍
TIERS: dict[str, Tier] = {
    "CHEAP": Tier(Provider.CLAUDE_CODE, "claude-sonnet-5", EffortLevel.LOW),
    "DEFAULT": Tier(Provider.CLAUDE_CODE, "claude-sonnet-5", EffortLevel.HIGH),
    "HIGH_CAPABILITY": Tier(Provider.CLAUDE_CODE, "claude-opus-5", EffortLevel.HIGH),
    "CODEX_DEFAULT": Tier(Provider.CODEX, "gpt-5.6-terra", EffortLevel.MEDIUM),
    "CODEX_HIGH_REASONING": Tier(Provider.CODEX, "gpt-5.6-sol", EffortLevel.HIGH),
}


def validate_combo(model: str, effort: str) -> None:
    supported = SUPPORT_MATRIX.get(model)
    if supported is None:
        raise RoutingValidationError(f"model not in support matrix: {model}")
    if effort not in supported:
        raise RoutingValidationError(
            f"unsupported (model, effort) combo: ({model}, {effort}); "
            f"supported: {sorted(supported)}"
        )


def resolve(tier_name: str, effort: EffortLevel | None = None) -> Resolved:
    tier = TIERS.get(tier_name)
    if tier is None:
        raise RoutingValidationError(f"unknown tier: {tier_name}")
    level = effort or tier.default_effort
    effort_str = _EFFORT_STR[level]
    validate_combo(tier.model, effort_str)
    return Resolved(tier.provider, tier.model, effort_str)
