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

    def create(self, name: str) -> Path:
        path = self.base / name
        self._git("worktree", "add", "-b", f"wt/{name}", str(path), "HEAD")
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
