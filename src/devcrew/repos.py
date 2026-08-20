"""Repo registry (#16) — 이름→경로 매핑. Slack 요청의 `이름:` 접두를 해석한다.

파일이 없으면 빈 registry (toy repo 전용 모드). 등록된 경로가 없거나 git repo가
아니면 로딩 시 RepoRegistryError로 fail-fast — 조용한 기본값 금지.

각 repo는 두 형태 중 하나로 쓴다:

    work-note: ~/Desktop/workspace/work-note          # base = 그 repo의 HEAD
    dev-crew:
      path: ~/Desktop/workspace/dev-crew
      branch: feat/orchestrator-loop                  # base = 이 브랜치

`branch`는 실행용 worktree(`wt/slack-N`)를 어느 커밋에서 딸지 정한다. 작업 코드가
아직 병합되지 않은 브랜치에 있으면 이걸 지정해야 한다 — HEAD(main)에서 따면
워커가 그 코드가 없는 트리에서 작업하게 된다 (2026-08-20 SLACK-3).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "repos.yaml"


class RepoRegistryError(Exception):
    pass


def _entry(name: str, loc) -> tuple[Path, str]:
    """registry 항목 → (경로, base ref). 문자열이면 base는 HEAD."""
    if isinstance(loc, dict):
        raw_path = loc.get("path")
        if not raw_path:
            raise RepoRegistryError(f"repo {name!r}: path 누락")
        base = str(loc.get("branch") or "HEAD")
    else:
        raw_path, base = str(loc), "HEAD"
    rp = Path(str(raw_path)).expanduser().resolve()
    if not rp.is_dir():
        raise RepoRegistryError(f"repo {name!r}: 경로 없음 — {rp}")
    if not (rp / ".git").exists():
        raise RepoRegistryError(f"repo {name!r}: git repo 아님 — {rp}")
    if base != "HEAD":
        r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
                           cwd=rp, capture_output=True, text=True)
        if r.returncode != 0:
            raise RepoRegistryError(f"repo {name!r}: 브랜치 {base!r}가 {rp}에 없다")
    return rp, base


def load_repos(path: str | Path | None = None) -> dict[str, Path]:
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text()) or {}
    return {str(n): _entry(str(n), loc)[0]
            for n, loc in (raw.get("repos") or {}).items()}


def load_repo_bases(path: str | Path | None = None) -> dict[str, str]:
    """repo 이름 → worktree base ref. 지정 없으면 "HEAD"."""
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text()) or {}
    return {str(n): _entry(str(n), loc)[1]
            for n, loc in (raw.get("repos") or {}).items()}


_REPO_HEAD_RE = re.compile(r"[A-Za-z0-9._@/-]+")


def split_repo_target(task: str, repos: dict[str, Path]) -> tuple[str | None, str | None, str]:
    """`이름: 작업` / `이름@브랜치: 작업` 접두 해석 → (repo_name, branch, 나머지 task).

    `@브랜치`는 이번 요청에 한해 worktree base를 덮어쓴다 — repos.yaml의 고정
    브랜치가 낡았을 때 설정을 고치지 않고 바로잡는 탈출구다 (2026-08-20 SLACK-3의
    재발 경로가 "설정 drift"이므로 요청 단위 지정이 필요하다).

    콜론 앞 토큰이 registry에 있을 때만 repo 지정으로 취급한다. registry에 없어도
    repo명 형태면 오타로 보고 RepoRegistryError를 던져 조용히 toy repo로 넘기지
    않는다. 그 외(한글 문장의 "주의: …" 등)는 일반 task로 통과.
    """
    head, sep, rest = task.partition(":")
    head = head.strip()
    if not sep or not head or " " in head:
        return None, None, task
    name, _at, branch = head.partition("@")
    name, branch = name.strip(), branch.strip() or None
    if name in repos:
        return name, branch, rest.strip()
    if _REPO_HEAD_RE.fullmatch(head):
        raise RepoRegistryError(
            f"등록되지 않은 repo {name!r} — 사용 가능: {', '.join(sorted(repos)) or '(없음)'}")
    return None, None, task


def split_repo_prefix(task: str, repos: dict[str, Path]) -> tuple[str | None, str]:
    """`split_repo_target`의 2-tuple 호환 래퍼 (브랜치 지정은 버린다)."""
    name, _branch, rest = split_repo_target(task, repos)
    return name, rest
