"""RepoRegistry — 대상 repository 지정/검증 (issue #16).

"기준 없는 작업 방지": repository를 지정하지 않은 요청은 provider 호출 전에
거부되어야 Slack으로 재질의할 수 있다. 이 모듈은 name → 절대경로 매핑을
provider 호출 이전에 검증하는 allowlist 역할을 한다.
"""
from __future__ import annotations

from pathlib import Path


class RepoRegistryError(ValueError):
    pass


class RepoRegistry:
    def __init__(self, mapping: dict[str, str | Path]):
        self._repos: dict[str, Path] = {}
        for name, raw_path in mapping.items():
            path = Path(raw_path)
            if not path.is_absolute():
                raise RepoRegistryError(
                    f"repo '{name}' has a relative path ({raw_path}); "
                    "registry entries must be absolute paths"
                )
            self._repos[name] = path

    def resolve(self, name: str | None) -> Path:
        if not name:
            raise RepoRegistryError(
                "request must name a target repository (no default repo allowed)"
            )
        path = self._repos.get(name)
        if path is None:
            raise RepoRegistryError(
                f"unknown repo: {name}; registered repos: {sorted(self._repos)}"
            )
        if not path.exists():
            raise RepoRegistryError(f"repo '{name}' path does not exist: {path}")
        if not (path / ".git").exists():
            raise RepoRegistryError(f"repo '{name}' path is not a git repository: {path}")
        return path.resolve()
