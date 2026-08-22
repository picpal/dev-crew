"""git worktree 관리 — Developer instance 격리 (§8.2)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def worktree_facts(path: str | Path) -> dict:
    """worktree의 객관적 상태 — 워커의 자기 진단을 대조할 근거.

    워커가 "대상 트리가 잘못 바인딩됐다" 같은 하네스 결함을 주장할 때 leader가
    검증 없이 믿으면 정상 실행이 ASK_USER로 끝난다 (2026-08-20 SLACK-3). 결정
    스냅샷에 이 사실을 함께 실어 주장과 실제를 대조하게 한다.
    """
    def git(*args: str) -> str:
        r = subprocess.run(["git", *args], cwd=str(path), capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""

    p = Path(path)
    dirty = git("status", "--porcelain")
    return {
        "path": str(p),
        "exists": p.is_dir(),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "head": git("rev-parse", "--short", "HEAD"),
        "head_subject": git("log", "-1", "--pretty=%s"),
        "tracked_files": len(git("ls-files").splitlines()),
        "dirty_files": len(dirty.splitlines()),
        "writable": os.access(p, os.W_OK) if p.is_dir() else False,
    }


class WorktreeManager:
    def __init__(self, repo_root: str | Path):
        self.repo = Path(repo_root)
        self.base = self.repo / ".worktrees"

    def _git(self, *args: str) -> str:
        out = subprocess.run(["git", *args], cwd=self.repo, check=True,
                             capture_output=True, text=True)
        return out.stdout

    def create(self, name: str, base_ref: str = "HEAD") -> Path:
        """`base_ref`에서 딴 새 worktree. 작업 코드가 아직 병합되지 않은 브랜치에
        있으면 그 브랜치를 base로 줘야 한다 — HEAD(main)에서 따면 워커가 그 코드가
        없는 트리에서 작업하게 된다 (2026-08-20 SLACK-3)."""
        path = self.base / name
        self._git("worktree", "add", "-b", f"wt/{name}", str(path), base_ref)
        return path

    def remove(self, name: str) -> None:
        self._git("worktree", "remove", "--force", str(self.base / name))
        self._git("branch", "-D", f"wt/{name}")

    def list_active(self) -> list[str]:
        out = self._git("worktree", "list", "--porcelain")
        names = []
        for line in out.splitlines():
            if line.startswith("worktree ") and "/.worktrees/" in line:
                names.append(line.rsplit("/", 1)[1])
        return names
