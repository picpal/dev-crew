"""POC 7 — 독립 worktree 2개에서 Developer(Claude) 병렬 실행, 충돌 없음 (§20)."""
import asyncio
import subprocess
import tempfile
from pathlib import Path

from _common import record, stores


async def main():
    from devcrew.adapters.claude_code import ClaudeCodeAdapter
    from devcrew.routing import resolve
    from devcrew.schema import AgentInstance, EffortLevel, Provider, Role
    from devcrew.worktree import WorktreeManager

    # 임시 git repo 준비
    repo = Path(tempfile.mkdtemp(prefix="poc4-repo-"))
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "README.md").write_text("# poc4\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=p@p", "-c", "user.name=p",
                    "commit", "-qm", "init"], cwd=repo, check=True)

    trace, registry = stores()
    wm = WorktreeManager(repo)
    adapter = ClaudeCodeAdapter(trace, registry)
    r = resolve("CHEAP")

    async def dev(name: str, filename: str):
        wt = wm.create(name)
        inst = AgentInstance(
            instance_id=f"POC4-{name}", role=Role.DEVELOPER,
            provider=Provider.CLAUDE_CODE, adapter="claude-code-adapter",
            model=r.model, effort_level=EffortLevel.LOW, reasoning_level=None,
            routing_policy_version="v1", routing_reason="poc4", session_id=None,
            execution_id="POC-4", workflow_id="POC", node_id=name,
            task_scope=filename, worktree=str(wt),
        )
        sid = await adapter.start_session(
            inst, f"너의 작업 디렉터리는 {wt}다. {wt}/{filename} 파일을 만들고 내용은 '{name}' 한 줄만 넣어. 끝나면 'OK'라고 답해.")
        await adapter.archive(sid)
        return wt

    wa, wb = await asyncio.gather(dev("dev-a", "feature_a.txt"), dev("dev-b", "feature_b.txt"))

    record("p04", {
        "dev_a_wrote_own_file": (wa / "feature_a.txt").exists(),
        "dev_b_wrote_own_file": (wb / "feature_b.txt").exists(),
        "no_cross_contamination": not (wa / "feature_b.txt").exists()
                                  and not (wb / "feature_a.txt").exists(),
    })


asyncio.run(main())
