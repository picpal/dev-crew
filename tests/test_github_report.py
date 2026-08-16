import subprocess

import pytest

from devcrew import github_report


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=stdout, stderr="")


def test_create_task_issue_retries_without_label_on_missing_label(monkeypatch):
    """Regression: task-report 라벨이 리포지토리에 없어도 이슈 생성은 성공해야 한다 (#9)."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--label" in cmd:
            raise subprocess.CalledProcessError(1, cmd, stderr="label not found")
        return _completed("https://github.com/picpal/dev-crew/issues/42\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    number = github_report.create_task_issue("T-1", "summary")

    assert number == 42
    assert any("--label" in c for c in calls)
    assert any("--label" not in c for c in calls)


def test_create_task_issue_succeeds_with_label_first_try(monkeypatch):
    def fake_run(cmd, **kwargs):
        assert "--label" in cmd
        return _completed("https://github.com/picpal/dev-crew/issues/7\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert github_report.create_task_issue("T-2", "summary") == 7


def test_create_task_issue_raises_on_non_numeric_url(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _completed("not-a-url\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="unexpected issue URL"):
        github_report.create_task_issue("T-3", "summary")
