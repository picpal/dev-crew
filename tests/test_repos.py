"""repos registry — 로딩 검증과 접두 해석."""
import subprocess

import pytest

from devcrew.repos import RepoRegistryError, load_repos, split_repo_prefix


def _git_repo(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    return d


def test_load_repos_ok(tmp_path):
    repo = _git_repo(tmp_path, "alpha")
    y = tmp_path / "repos.yaml"
    y.write_text(f"repos:\n  alpha: {repo}\n")
    assert load_repos(y) == {"alpha": repo.resolve()}


def test_load_repos_missing_file_is_empty(tmp_path):
    assert load_repos(tmp_path / "none.yaml") == {}


def test_load_repos_nonexistent_path_fails(tmp_path):
    y = tmp_path / "repos.yaml"
    y.write_text("repos:\n  ghost: /no/such/dir\n")
    with pytest.raises(RepoRegistryError, match="경로 없음"):
        load_repos(y)


def test_load_repos_non_git_fails(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    y = tmp_path / "repos.yaml"
    y.write_text(f"repos:\n  plain: {d}\n")
    with pytest.raises(RepoRegistryError, match="git repo 아님"):
        load_repos(y)


def test_split_prefix_known_repo():
    assert split_repo_prefix("alpha: 작업 내용", {"alpha": None}) == ("alpha", "작업 내용")


def test_split_prefix_korean_sentence_passes_through():
    assert split_repo_prefix("주의: 이건 일반 문장", {"alpha": None}) == (None, "주의: 이건 일반 문장")


def test_split_prefix_typo_fails():
    with pytest.raises(RepoRegistryError, match="alphaa"):
        split_repo_prefix("alphaa: 오타", {"alpha": None})


def test_split_prefix_no_colon():
    assert split_repo_prefix("그냥 작업", {"alpha": None}) == (None, "그냥 작업")


def _init_repo(path):
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "a.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=path, check=True)
    return path


def test_repo_entry_accepts_branch_and_reports_base(tmp_path):
    """작업 코드가 미병합 브랜치에 있으면 worktree base를 그 브랜치로 지정한다
    (미지정 시 HEAD에서 따 워커가 코드 없는 트리에서 작업한다 — SLACK-3)."""
    import subprocess
    from devcrew.repos import load_repo_bases, load_repos
    repo = _init_repo(tmp_path / "r")
    subprocess.run(["git", "branch", "feature/x"], cwd=repo, check=True)
    cfg = tmp_path / "repos.yaml"
    cfg.write_text(f"repos:\n  plain: {repo}\n  pinned:\n    path: {repo}\n"
                   f"    branch: feature/x\n")
    assert load_repos(cfg) == {"plain": repo.resolve(), "pinned": repo.resolve()}
    assert load_repo_bases(cfg) == {"plain": "HEAD", "pinned": "feature/x"}


def test_unknown_branch_fails_fast(tmp_path):
    from devcrew.repos import RepoRegistryError, load_repos
    repo = _init_repo(tmp_path / "r")
    cfg = tmp_path / "repos.yaml"
    cfg.write_text(f"repos:\n  x:\n    path: {repo}\n    branch: 없는브랜치\n")
    with pytest.raises(RepoRegistryError, match="없는브랜치"):
        load_repos(cfg)


def test_worktree_created_from_given_base_ref(tmp_path):
    import subprocess
    from devcrew.worktree import WorktreeManager
    repo = _init_repo(tmp_path / "r")
    (repo / "only-on-branch.txt").write_text("y")
    subprocess.run(["git", "checkout", "-qb", "feature/x"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "branch work"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "master" if
                    subprocess.run(["git", "rev-parse", "--verify", "-q", "master"],
                                   cwd=repo, capture_output=True).returncode == 0
                    else "main"], cwd=repo, check=True)

    wt = WorktreeManager(repo).create("t1", "feature/x")
    assert (wt / "only-on-branch.txt").exists()      # 브랜치 커밋이 들어있다
    plain = WorktreeManager(repo).create("t2")       # 기본 HEAD
    assert not (plain / "only-on-branch.txt").exists()
