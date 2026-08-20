"""git worktree 관리 — Developer instance 격리 (§8.2)."""
from __future__ import annotations

import subprocess
from pathlib import Path


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
