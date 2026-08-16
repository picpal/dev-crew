import subprocess

from devcrew.worktree import WorktreeManager


def _git_repo(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_create_two_isolated_worktrees(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    wm = WorktreeManager(repo)
    wa, wb = wm.create("dev-a"), wm.create("dev-b")
    assert wa != wb and wa.exists() and wb.exists()
    # 서로 다른 파일을 수정해도 충돌하지 않는다
    (wa / "feat_a.txt").write_text("a")
    (wb / "feat_b.txt").write_text("b")
    assert not (wa / "feat_b.txt").exists()
    assert sorted(wm.list_active()) == ["dev-a", "dev-b"]
    wm.remove("dev-a")
    assert wm.list_active() == ["dev-b"]
