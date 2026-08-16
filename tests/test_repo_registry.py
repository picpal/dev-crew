import subprocess
from pathlib import Path

import pytest
from devcrew.repo_registry import RepoRegistry, RepoRegistryError


def _git_repo(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def test_resolve_happy_path(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    registry = RepoRegistry({"dev-crew": repo})
    resolved = registry.resolve("dev-crew")
    assert resolved == repo.resolve()
    assert resolved.is_absolute()


def test_resolve_unknown_name_lists_registered_names(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    registry = RepoRegistry({"dev-crew": repo})
    with pytest.raises(RepoRegistryError) as exc_info:
        registry.resolve("no-such-repo")
    message = str(exc_info.value)
    assert "no-such-repo" in message
    assert "dev-crew" in message


def test_resolve_missing_path_on_disk(tmp_path):
    missing = tmp_path / "does-not-exist"
    registry = RepoRegistry({"dev-crew": missing})
    with pytest.raises(RepoRegistryError):
        registry.resolve("dev-crew")


def test_resolve_path_not_a_git_repo(tmp_path):
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    registry = RepoRegistry({"dev-crew": plain_dir})
    with pytest.raises(RepoRegistryError):
        registry.resolve("dev-crew")


def test_resolve_accepts_worktree_with_dotgit_file(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    worktree_path = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "wt-branch", str(worktree_path), "HEAD"],
        cwd=repo,
        check=True,
    )
    assert (worktree_path / ".git").is_file()
    registry = RepoRegistry({"dev-crew-wt": worktree_path})
    resolved = registry.resolve("dev-crew-wt")
    assert resolved == worktree_path.resolve()


def test_constructor_rejects_relative_paths():
    with pytest.raises(RepoRegistryError):
        RepoRegistry({"dev-crew": "relative/path"})


def test_constructor_rejects_relative_path_objects():
    with pytest.raises(RepoRegistryError):
        RepoRegistry({"dev-crew": Path("relative/path")})


def test_resolve_none_rejected():
    registry = RepoRegistry({})
    with pytest.raises(RepoRegistryError):
        registry.resolve(None)


def test_resolve_empty_string_rejected():
    registry = RepoRegistry({})
    with pytest.raises(RepoRegistryError):
        registry.resolve("")


def test_repo_registry_error_is_value_error(tmp_path):
    assert issubclass(RepoRegistryError, ValueError)
