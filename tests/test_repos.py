"""repos registry — 로딩 검증과 접두 해석."""
import subprocess
from pathlib import Path

import pytest

from devcrew.repos import (RepoRegistryError, format_repo_names, load_repo_bases, load_repos,
                           split_repo_prefix, split_repo_target)


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


@pytest.mark.parametrize("bad", ["--force", "-f", "--detach", "..", "a..b", "feat/",
                                 "x.lock", "$(whoami)"])
def test_branch_that_git_would_read_as_option_is_rejected(bad):
    """`repo@branch:`의 branch는 그대로 `git worktree add ... <ref>` 인자가 된다.
    shell을 안 거쳐 명령 주입은 불가하지만 `-f`/`--force` 등은 git이 **옵션으로**
    해석해 명령 의미를 바꾼다 — 파싱 단계에서 막는다."""
    from devcrew.repos import RepoRegistryError, split_repo_target
    with pytest.raises(RepoRegistryError):
        split_repo_target(f"dev-crew@{bad}: 작업", {"dev-crew": Path("/x")})


def test_valid_branch_prefix_passes_through():
    from devcrew.repos import split_repo_target
    repos = {"dev-crew": Path("/x")}
    assert split_repo_target("dev-crew@feat/orch-loop.2: 작업", repos) == (
        "dev-crew", "feat/orch-loop.2", "작업")
    assert split_repo_target("dev-crew: 작업", repos) == ("dev-crew", None, "작업")


def test_config_branch_is_validated_too(tmp_path):
    from devcrew.repos import RepoRegistryError, load_repos
    repo = _init_repo(tmp_path / "r")
    cfg = tmp_path / "repos.yaml"
    cfg.write_text(f"repos:\n  x:\n    path: {repo}\n    branch: '--force'\n")
    with pytest.raises(RepoRegistryError, match="쓸 수 없는 값"):
        load_repos(cfg)


# ── workspace 자동 탐색 ──────────────────────────────────────────────────────

def test_workspace_root_registers_every_git_child(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    a, b = _git_repo(ws, "alpha"), _git_repo(ws, "beta")
    (ws / "not-a-repo").mkdir()          # git repo가 아니면 조용히 건너뛴다
    y = tmp_path / "repos.yaml"
    y.write_text(f"workspace_roots:\n  - {ws}\n")
    assert load_repos(y) == {"alpha": a.resolve(), "beta": b.resolve()}


def test_workspace_root_skips_hidden_and_unaddressable_names(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    ok = _git_repo(ws, "alpha")
    _git_repo(ws, ".hidden")             # 점으로 시작하면 작업 디렉토리가 아니다
    _git_repo(ws, "has space")           # `이름:` 접두로 쓸 수 없는 이름
    y = tmp_path / "repos.yaml"
    y.write_text(f"workspace_roots:\n  - {ws}\n")
    assert load_repos(y) == {"alpha": ok.resolve()}


def test_explicit_entry_wins_over_discovered(tmp_path):
    """자동 탐색은 기본값이다 — 브랜치를 고정한 명시 등록을 덮어쓰면 안 된다."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    repo = _git_repo(ws, "alpha")
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=repo, check=True)
    subprocess.run(["git", "branch", "work"], cwd=repo, check=True)
    y = tmp_path / "repos.yaml"
    y.write_text(f"workspace_roots:\n  - {ws}\n"
                 f"repos:\n  alpha:\n    path: {repo}\n    branch: work\n")
    assert load_repos(y) == {"alpha": repo.resolve()}
    assert load_repo_bases(y)["alpha"] == "work"


def test_discovered_repo_base_is_head(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    _git_repo(ws, "alpha")
    y = tmp_path / "repos.yaml"
    y.write_text(f"workspace_roots:\n  - {ws}\n")
    assert load_repo_bases(y) == {"alpha": "HEAD"}


def test_missing_workspace_root_fails_loudly(tmp_path):
    """탐색 실패는 조용한 빈 목록이 되면 안 된다 — 전 repo가 사라진 것처럼 보인다."""
    y = tmp_path / "repos.yaml"
    y.write_text("workspace_roots:\n  - /no/such/workspace\n")
    with pytest.raises(RepoRegistryError, match="workspace_root"):
        load_repos(y)


def test_format_repo_names_caps_the_listing():
    names = {f"repo-{i:02d}": None for i in range(30)}
    out = format_repo_names(names, limit=5)
    assert "repo-00" in out and "repo-29" not in out
    assert "외 25개" in out


def test_format_repo_names_short_list_has_no_tail():
    assert format_repo_names({"a": None, "b": None}, limit=5) == "a, b"


def test_format_repo_names_empty():
    assert format_repo_names({}, limit=5) == "(없음)"


# ── 미등록 repo → leader 판단으로 생성 ────────────────────────────────────────
def test_unknown_repo_error_carries_the_name(tmp_path):
    """문자열 예외로는 어느 이름이었는지 못 꺼낸다 — leader에게 물어보려면 필요하다."""
    from devcrew.repos import UnknownRepoError
    with pytest.raises(UnknownRepoError) as ei:
        split_repo_target("todo-web: 만들어줘", {"alpha": tmp_path})
    assert ei.value.name == "todo-web"
    assert ei.value.available == ["alpha"]
    assert isinstance(ei.value, RepoRegistryError)      # 기존 처리 경로 유지


def test_create_repo_makes_a_committed_repo(tmp_path):
    """`worktree add ... HEAD`는 커밋이 0개면 실패한다 — 초기 커밋까지 만들어야 한다."""
    from devcrew.repos import create_repo
    ws = tmp_path / "workspace"
    ws.mkdir()
    path = create_repo("todo-web", [ws])
    assert path == (ws / "todo-web").resolve()
    assert (path / ".git").exists()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path,
                          capture_output=True, text=True)
    assert head.returncode == 0, "커밋이 없으면 worktree를 딸 수 없다"
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path,
                            capture_output=True, text=True).stdout.strip()
    assert branch == "main"


def test_created_repo_is_immediately_discoverable(tmp_path):
    """생성해 놓고 registry에 안 잡히면 방금 만든 repo를 또 '미등록'이라 한다."""
    from devcrew.repos import create_repo
    ws = tmp_path / "workspace"
    ws.mkdir()
    y = tmp_path / "repos.yaml"
    y.write_text(f"workspace_roots:\n  - {ws}\n")
    assert load_repos(y) == {}
    create_repo("todo-web", [ws])
    assert set(load_repos(y)) == {"todo-web"}


def test_create_repo_refuses_to_escape_the_workspace(tmp_path):
    """이름은 Slack 사용자가 주는 값이다 — 경로 조작이 통하면 안 된다."""
    from devcrew.repos import create_repo
    ws = tmp_path / "workspace"
    ws.mkdir()
    for bad in ("../escape", "/etc/passwd", "a/b", ".hidden", "-rf", ""):
        with pytest.raises(RepoRegistryError):
            create_repo(bad, [ws])
    assert list(ws.iterdir()) == []


def test_create_repo_refuses_an_existing_path(tmp_path):
    from devcrew.repos import create_repo
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "taken").mkdir()
    with pytest.raises(RepoRegistryError, match="이미"):
        create_repo("taken", [ws])


def test_create_repo_needs_a_workspace_root(tmp_path):
    """루트가 없으면 어디에 만들지 모른다 — 조용히 cwd에 만들면 안 된다."""
    from devcrew.repos import create_repo
    with pytest.raises(RepoRegistryError, match="workspace_root"):
        create_repo("todo-web", [])
