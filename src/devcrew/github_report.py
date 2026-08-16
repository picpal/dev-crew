"""GitHub Task Report (§14) — gh CLI 래퍼. POC는 생성과 최종 갱신만."""
from __future__ import annotations

import json
import subprocess


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True,
                          capture_output=True, text=True).stdout


def create_task_issue(task_id: str, summary: str) -> int:
    out = _gh("issue", "create", "--title", f"[{task_id}] {summary}",
              "--body", f"# Task Report\n\n- Task ID: {task_id}\n- Status: IN_PROGRESS",
              "--label", "task-report")
    return int(out.strip().rsplit("/", 1)[1])


def finalize_task_issue(number: int, *, report_url: str, status: str) -> None:
    _gh("issue", "comment", str(number), "--body",
        f"## 최종 결과\n- Status: {status}\n- Detailed HTML Report: {report_url}")
    if status == "DONE":
        _gh("issue", "close", str(number))
