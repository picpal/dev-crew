"""GitHub Task Report (§14) — gh CLI 래퍼. POC는 생성과 최종 갱신만."""
from __future__ import annotations

import json
import subprocess


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True,
                          capture_output=True, text=True).stdout


def create_task_issue(task_id: str, summary: str) -> int:
    """이슈를 생성하고 번호를 반환한다.

    운영자 체크리스트: `task-report` 라벨을 리포지토리에 미리 만들어 두면
    (`gh label create task-report`) 매 이슈에 라벨이 붙는다. 라벨이 없으면
    `gh issue create --label`이 즉시 실패하므로, 라벨 없이 한 번 더 시도한다.
    """
    title = ["issue", "create", "--title", f"[{task_id}] {summary}",
             "--body", f"# Task Report\n\n- Task ID: {task_id}\n- Status: IN_PROGRESS"]
    try:
        out = _gh(*title, "--label", "task-report")
    except subprocess.CalledProcessError:
        out = _gh(*title)
    url = out.strip()
    number = url.rsplit("/", 1)[-1]
    if not number.isdigit():
        raise ValueError(f"unexpected issue URL from gh issue create: {url!r}")
    return int(number)


def finalize_task_issue(number: int, *, report_url: str, status: str) -> None:
    _gh("issue", "comment", str(number), "--body",
        f"## 최종 결과\n- Status: {status}\n- Detailed HTML Report: {report_url}")
    if status == "DONE":
        _gh("issue", "close", str(number))
