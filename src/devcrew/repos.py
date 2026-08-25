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

`workspace_roots`를 주면 그 디렉토리 **바로 아래**의 git repo를 모두 디렉토리명으로
자동 등록한다. repo 하나 늘 때마다 설정을 고치는 일을 없애기 위한 것이다 — 등록되지
않았다는 이유로 요청이 반려되던 마찰이 사라진다.

    workspace_roots:
      - ~/Desktop/workspace

자동 등록분의 base는 언제나 HEAD다. 브랜치를 고정해야 하는 repo는 `repos:`에 명시로
적는다 — **명시 등록이 자동 탐색을 이긴다.**
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "repos.yaml"

# 새 repo의 기본 브랜치와, 사용자 git identity가 없을 때의 폴백 커밋 작성자.
DEFAULT_BRANCH = "main"
_FALLBACK_NAME = "dev-crew"
_FALLBACK_EMAIL = "dev-crew@localhost"


class RepoRegistryError(Exception):
    pass


class UnknownRepoError(RepoRegistryError):
    """`이름:` 접두가 registry에 없다. 오타일 수도, 아직 없는 새 repo일 수도 있다.

    어느 쪽인지는 요청 본문을 봐야 안다("todo 웹앱 만들어줘" vs `dev-crw:`) — 그래서
    이름과 후보 목록을 예외에 실어 leader가 판단할 수 있게 한다. 문자열 메시지만으로는
    호출자가 이름을 다시 꺼낼 방법이 없다.
    """

    def __init__(self, name: str, available):
        self.name = name
        self.available = sorted(available)
        super().__init__(f"등록되지 않은 repo {name!r} — "
                         f"사용 가능: {format_repo_names(self.available)}")


def _entry(name: str, loc) -> tuple[Path, str]:
    """registry 항목 → (경로, base ref). 문자열이면 base는 HEAD."""
    if isinstance(loc, dict):
        raw_path = loc.get("path")
        if not raw_path:
            raise RepoRegistryError(f"repo {name!r}: path 누락")
        base = str(loc.get("branch") or "HEAD")
        if base != "HEAD":
            validate_ref(name, base)
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


# 자동 등록 이름은 Slack의 `이름:` 접두로 그대로 쓰인다. 공백·한글·`@`가 든 디렉토리는
# 접두로 지목할 수 없으므로 (`@`는 `이름@브랜치` 구분자다) 탐색에서 제외한다.
_AUTO_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def discover_repos(roots) -> dict[str, tuple[Path, str]]:
    """workspace_roots 바로 아래의 git repo를 이름→(경로, "HEAD")로 모은다.

    루트가 없으면 fail-fast한다. 오타 난 루트를 조용히 건너뛰면 등록된 repo가 통째로
    사라진 채로 엔진이 뜨고, 모든 요청이 "등록되지 않은 repo"로 반려된다.
    """
    found: dict[str, tuple[Path, str]] = {}
    for raw in roots or []:
        root = Path(str(raw)).expanduser()
        if not root.is_dir():
            raise RepoRegistryError(f"workspace_root 경로 없음 — {root}")
        for child in sorted(root.iterdir()):
            name = child.name
            if name.startswith(".") or not _AUTO_NAME_RE.fullmatch(name):
                continue
            if not child.is_dir() or not (child / ".git").exists():
                continue
            found.setdefault(name, (child.resolve(), "HEAD"))
    return found


def _registry(path: str | Path | None = None) -> dict[str, tuple[Path, str]]:
    """설정 파일 → 이름→(경로, base ref). 명시 등록이 자동 탐색을 덮어쓴다."""
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text()) or {}
    reg = discover_repos(raw.get("workspace_roots"))
    for n, loc in (raw.get("repos") or {}).items():
        reg[str(n)] = _entry(str(n), loc)
    return dict(sorted(reg.items()))


def load_repos(path: str | Path | None = None) -> dict[str, Path]:
    return {n: v[0] for n, v in _registry(path).items()}


def load_repo_bases(path: str | Path | None = None) -> dict[str, str]:
    """repo 이름 → worktree base ref. 지정 없으면 "HEAD"."""
    return {n: v[1] for n, v in _registry(path).items()}


def format_repo_names(repos, limit: int = 12) -> str:
    """사람에게 보여줄 repo 목록. 자동 등록이면 수십 개라 Slack 한 줄을 넘긴다."""
    names = sorted(repos)
    if not names:
        return "(없음)"
    if len(names) <= limit:
        return ", ".join(names)
    return f"{', '.join(names[:limit])} … 외 {len(names) - limit}개"


_REPO_HEAD_RE = re.compile(r"[A-Za-z0-9._@/-]+")
# branch/ref는 그대로 `git worktree add ... <ref>`의 인자가 된다. shell을 거치지
# 않으므로 명령 주입은 불가하지만, `-f`/`--force`/`--detach` 같은 값은 git이
# **옵션으로 해석**해 명령 의미를 바꾼다 — 선행 '-'를 막고 ref 문자 집합으로 제한한다.
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def validate_ref(name: str, ref: str) -> str:
    if not _REF_RE.fullmatch(ref) or ".." in ref or ref.endswith(("/", ".lock")):
        raise RepoRegistryError(
            f"repo {name!r}: 브랜치 이름으로 쓸 수 없는 값 {ref!r} "
            "(영숫자로 시작, 영숫자/._/- 만 허용)")
    return ref


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
        if branch is not None:
            validate_ref(name, branch)      # git 인자 인젝션 차단 (선행 '-' 등)
        return name, branch, rest.strip()
    if _REPO_HEAD_RE.fullmatch(head):
        raise UnknownRepoError(name, repos)
    return None, None, task


def split_repo_prefix(task: str, repos: dict[str, Path]) -> tuple[str | None, str]:
    """`split_repo_target`의 2-tuple 호환 래퍼 (브랜치 지정은 버린다)."""
    name, _branch, rest = split_repo_target(task, repos)
    return name, rest


def workspace_roots(path: str | Path | None = None) -> list[Path]:
    """설정의 workspace_roots — 새 repo를 만들 수 있는 유일한 자리."""
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text()) or {}
    return [Path(str(r)).expanduser() for r in (raw.get("workspace_roots") or [])]


def create_repo(name: str, roots) -> Path:
    """`roots[0]` 바로 아래에 새 git repo를 만든다. 경로를 반환한다.

    **초기 커밋까지 만든다.** 커밋이 0개면 HEAD가 해석되지 않아
    `git worktree add ... HEAD`가 실패하고, 실행이 repo를 만들자마자 죽는다.

    `name`은 Slack 사용자가 준 값이다 — 자동 등록 이름 규칙(`_AUTO_NAME_RE`)으로
    제한해 경로 조작(`../`, 절대경로, 숨김 디렉토리)을 막고, 만든 뒤 결과 경로가
    루트 **바로 아래**인지 다시 확인한다.
    """
    roots = [Path(r).expanduser() for r in (roots or [])]
    if not roots:
        raise RepoRegistryError(
            "workspace_root가 없어 새 repo를 만들 자리가 없다 — config/repos.yaml 참조")
    if not _AUTO_NAME_RE.fullmatch(name or ""):
        raise RepoRegistryError(
            f"repo 이름으로 쓸 수 없는 값 {name!r} (영숫자로 시작, 영숫자/._- 만 허용)")

    root = roots[0]
    if not root.is_dir():
        raise RepoRegistryError(f"workspace_root 경로 없음 — {root}")
    target = (root / name).resolve()
    if target.parent != root.resolve():        # 이름 규칙을 통과해도 한 번 더 본다
        raise RepoRegistryError(f"repo {name!r}: workspace 밖을 가리킨다 — {target}")
    if target.exists():
        raise RepoRegistryError(f"repo {name!r}: 이미 있다 — {target}")

    def git(*args: str, **kw) -> None:
        r = subprocess.run(["git", *args], cwd=kw.get("cwd", target),
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RepoRegistryError(
                f"repo {name!r} 생성 실패: git {' '.join(args)} — {r.stderr.strip()}")

    target.mkdir(parents=True)
    try:
        git("init", "-b", DEFAULT_BRANCH)
        (target / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        git("add", "README.md")
        # 사용자 전역 git identity가 없으면 commit이 실패한다. 있으면 그걸 쓰고,
        # 없을 때만 하네스 이름으로 채운다 — 사용자 설정을 덮어쓰지 않는다.
        ident = subprocess.run(["git", "config", "user.email"], cwd=target,
                               capture_output=True, text=True).stdout.strip()
        pre = [] if ident else ["-c", f"user.name={_FALLBACK_NAME}",
                                "-c", f"user.email={_FALLBACK_EMAIL}"]
        git(*pre, "commit", "-m", f"init: {name}")
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)   # 반쯤 만들어진 repo를 남기지 않는다
        raise
    return target
