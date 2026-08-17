"""Role 번들 로더 — 지침(prompt.md) + 출력 계약(output.schema.json).

Adapter는 role을 모른다: spawn이 번들을 로드해 전달만 한다 (spec 결정 2).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .schema import Role

ROLES_DIR = Path(__file__).resolve().parents[2] / "roles"
STATUS_ENUM = ["PASS", "NOT_PASS", "NEED_REPLAN", "BLOCKED", "INSUFFICIENT_CAPABILITY"]


class RoleBundleError(Exception):
    pass


@dataclass(frozen=True)
class RoleBundle:
    role: Role
    prompt: str
    schema: dict
    version: str


def load_bundle(role: Role, roles_dir: str | Path | None = None) -> RoleBundle:
    base = Path(roles_dir) if roles_dir else ROLES_DIR
    d = base / role.value.lower()
    prompt_p, schema_p = d / "prompt.md", d / "output.schema.json"
    if not prompt_p.exists() or not schema_p.exists():
        raise RoleBundleError(f"role bundle incomplete for {role.value}: {d}")
    prompt = prompt_p.read_text()
    schema = json.loads(schema_p.read_text())
    _validate_skeleton(role, schema)
    version = hashlib.sha256((prompt + json.dumps(schema, sort_keys=True)).encode()).hexdigest()[:12]
    return RoleBundle(role=role, prompt=prompt, schema=schema, version=version)


def _validate_skeleton(role: Role, schema: dict) -> None:
    props = schema.get("properties") or {}
    status = props.get("status") or {}
    if status.get("enum") != STATUS_ENUM:
        raise RoleBundleError(f"{role.value}: status enum mismatch")
    if "summary" not in props or "status" not in (schema.get("required") or []):
        raise RoleBundleError(f"{role.value}: common skeleton missing")
