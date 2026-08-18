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


def missing_required_keys(schema: dict, data, *, path: str = "") -> list[str]:
    """경량 required-키 재귀 검사 (finding #2/#3, wave 2 F2) — jsonschema 의존성
    없이 schema의 `required` 목록에 있는 키가 `data`에 전부 존재하는지만
    확인한다. 값이 dict인 property는 그 object schema의 `required`도 재귀
    확인하고, 값이 list인 array property는 그 `items`가 object schema일 때 리스트의
    각 원소에 대해서도 재귀 확인한다(예: REVIEWER의 `findings[i]`, QA의
    `results[i]`, EXPLORER의 `findings[i]`) — wave 1은 object property만 재귀해
    `{"findings": [{}]}`처럼 배열 원소 내부의 필수 키 누락을 놓쳤다. 타입 검증은
    하지 않는다 — provider 네이티브 출력 강제(output_schema)가 1차 방어라는 기존
    spec 결정을 유지하고, 여기서는 그 강제를 우회한 malformed 출력(예: 필수 필드
    누락)만 잡는 2차 방어다.

    반환값은 누락된 키의 경로 목록이다 (예: `["decision.rationale"]`,
    `["findings[0].severity"]`). 빈 리스트면 통과. `data`가 dict가 아니면 스키마
    자체가 object를 기대하므로 그 지점을 통째로 누락 취급한다.
    """
    if not isinstance(data, dict):
        return [path or "$"]
    missing: list[str] = []
    for key in schema.get("required") or []:
        if key not in data:
            missing.append(f"{path}.{key}" if path else key)
    for key, subschema in (schema.get("properties") or {}).items():
        if not isinstance(subschema, dict) or key not in data:
            continue
        value = data[key]
        subpath = f"{path}.{key}" if path else key
        if subschema.get("type") == "object" and isinstance(value, dict):
            missing.extend(missing_required_keys(subschema, value, path=subpath))
        elif subschema.get("type") == "array" and isinstance(value, list):
            items_schema = subschema.get("items")
            if isinstance(items_schema, dict) and items_schema.get("type") == "object":
                for i, item in enumerate(value):
                    missing.extend(missing_required_keys(
                        items_schema, item, path=f"{subpath}[{i}]"))
    return missing
