"""Repo registry (#16) — 이름→경로 매핑. Slack 요청의 `이름:` 접두를 해석한다.

파일이 없으면 빈 registry (toy repo 전용 모드). 등록된 경로가 없거나 git repo가
아니면 로딩 시 RepoRegistryError로 fail-fast — 조용한 기본값 금지.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "repos.yaml"


class RepoRegistryError(Exception):
    pass


def load_repos(path: str | Path | None = None) -> dict[str, Path]:
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text()) or {}
    repos = raw.get("repos") or {}
    out: dict[str, Path] = {}
    for name, loc in repos.items():
        rp = Path(str(loc)).expanduser().resolve()
        if not rp.is_dir():
            raise RepoRegistryError(f"repo {name!r}: 경로 없음 — {rp}")
        if not (rp / ".git").exists():
            raise RepoRegistryError(f"repo {name!r}: git repo 아님 — {rp}")
        out[str(name)] = rp
    return out


def split_repo_prefix(task: str, repos: dict[str, Path]) -> tuple[str | None, str]:
    """`이름: 작업` 접두 해석 → (repo_name | None, 나머지 task).

    콜론 앞 토큰이 registry에 있을 때만 repo 지정으로 취급한다. registry에 없어도
    repo명 형태(영숫자/._-)면 오타로 보고 RepoRegistryError를 던져 조용히 toy
    repo로 넘기지 않는다. 그 외(한글 문장의 "주의: …" 등)는 일반 task로 통과.
    """
    head, sep, rest = task.partition(":")
    head = head.strip()
    if not sep or not head or " " in head:
        return None, task
    if head in repos:
        return head, rest.strip()
    if re.fullmatch(r"[A-Za-z0-9._-]+", head):
        raise RepoRegistryError(
            f"등록되지 않은 repo {head!r} — 사용 가능: {', '.join(sorted(repos)) or '(없음)'}")
    return None, task
