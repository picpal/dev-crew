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
