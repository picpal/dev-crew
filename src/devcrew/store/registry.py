"""harness.db — 활성-only Session Registry (#15).

살아있는 instance 행만 보관한다. 이력은 trace.db가 담당.
lazy verify: 재시작 시 전제조건만 검증, LLM 호출 0회.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Callable

from ..schema import AgentInstance

_SCHEMA = """
CREATE TABLE IF NOT EXISTS registry (
    instance_id TEXT PRIMARY KEY,
    body TEXT NOT NULL,          -- AgentInstance JSON
    provider_ref TEXT,           -- Claude transcript 경로 / Codex thread_id
    last_summary TEXT,
    artifacts TEXT NOT NULL DEFAULT '[]'
);
"""


class SessionRegistry:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._con = sqlite3.connect(self.path)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(_SCHEMA)
        self._con.commit()

    def upsert(self, inst: AgentInstance, provider_ref: str | None) -> None:
        self._con.execute(
            "INSERT INTO registry (instance_id, body, provider_ref) VALUES (?, ?, ?)"
            " ON CONFLICT(instance_id) DO UPDATE SET body=excluded.body,"
            " provider_ref=excluded.provider_ref",
            (inst.instance_id, inst.to_json(), provider_ref),
        )
        self._con.commit()

    def checkpoint(self, instance_id: str, *, summary: str, artifacts: list[str]) -> None:
        """turn 체크포인트(#15) — 매 turn 완료 시 호출된다."""
        self._con.execute(
            "UPDATE registry SET last_summary=?, artifacts=? WHERE instance_id=?",
            (summary, json.dumps(artifacts, ensure_ascii=False), instance_id),
        )
        self._con.commit()

    def finish(self, instance_id: str) -> None:
        self._con.execute("DELETE FROM registry WHERE instance_id=?", (instance_id,))
        self._con.commit()

    def active(self) -> list[dict]:
        rows = self._con.execute(
            "SELECT instance_id, body, provider_ref, last_summary, artifacts FROM registry"
        ).fetchall()
        return [
            {"instance_id": r[0], "body": json.loads(r[1]), "provider_ref": r[2],
             "last_summary": r[3], "artifacts": json.loads(r[4])}
            for r in rows
        ]

    def lazy_verify(self, verifiers: dict[str, Callable[[dict], bool]]) -> dict[str, str]:
        out: dict[str, str] = {}
        for row in self.active():
            provider = row["body"]["provider"]
            check = verifiers.get(provider, lambda _row: False)
            out[row["instance_id"]] = "RESUMABLE" if check(row) else "FAILED_RECOVERY"
        return out

    def handoff(self, instance_id: str) -> dict:
        """7.6절 escalation 인계 스키마 — recovery와 공용 (#15). reason은 호출자가 채운다."""
        try:
            row = next(r for r in self.active() if r["instance_id"] == instance_id)
        except StopIteration:
            raise KeyError(f"instance {instance_id} not in registry") from None
        body = row["body"]
        return {
            "task_scope": body["task_scope"],
            "acceptance_criteria": None,
            "last_summary": row["last_summary"],
            "artifacts": row["artifacts"],
            "failure_evidence": None,
            "worktree": body["worktree"],
            "reason": None,
        }
