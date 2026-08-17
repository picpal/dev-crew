"""§17 설정 로딩 — 설정이 정본, 코드 폴백 없음."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .schema import EffortLevel, Provider, Role

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "harness.yaml"


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
class HarnessConfig:
    tiers: dict[str, TierSpec]
    role_defaults: dict[Role, RoleDefault]


def load(path: str | Path | None = None) -> HarnessConfig:
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        raise ConfigError(f"config not found: {p}")
    raw = yaml.safe_load(p.read_text())
    try:
        tiers = {
            name: TierSpec(Provider(t["provider"]), t["model"], EffortLevel(t["effort"]))
            for name, t in (raw.get("tiers") or {}).items()
        }
        role_defaults = {
            Role(name): RoleDefault(d["tier"],
                                    EffortLevel(d["effort"]) if d.get("effort") else None)
            for name, d in (raw.get("roleDefaults") or {}).items()
        }
    except (KeyError, ValueError) as e:
        raise ConfigError(f"invalid config {p}: {e}") from e
    if not tiers:
        raise ConfigError(f"config has no tiers: {p}")
    return HarnessConfig(tiers=tiers, role_defaults=role_defaults)
