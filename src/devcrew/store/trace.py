"""trace.db — append-only events가 진실, projection은 재구축 가능 (#7)."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from ..schema import AgentInstance

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    event_type TEXT NOT NULL,
    task_id TEXT NOT NULL,
    execution_id TEXT,
    instance_id TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_exec ON events(execution_id);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TABLE IF NOT EXISTS agent_instances (
    instance_id TEXT PRIMARY KEY,
    body TEXT NOT NULL
);
"""


class TraceStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._con = sqlite3.connect(self.path)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(_SCHEMA)
        self._con.commit()

    def append(self, event_type: str, *, task_id: str, payload: dict,
               execution_id: str | None = None, instance_id: str | None = None) -> int:
        cur = self._con.execute(
            "INSERT INTO events (ts, event_type, task_id, execution_id, instance_id, payload)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), event_type, task_id, execution_id, instance_id,
             json.dumps(payload, ensure_ascii=False)),
        )
        self._con.commit()
        return cur.lastrowid

    def events(self, *, event_type: str | None = None,
               execution_id: str | None = None) -> list[dict]:
        q, args = ("SELECT id, ts, event_type, task_id, execution_id, instance_id, payload"
                   " FROM events"), []
        conds = []
        if event_type:
            conds.append("event_type = ?"); args.append(event_type)
        if execution_id:
            conds.append("execution_id = ?"); args.append(execution_id)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY id"
        rows = self._con.execute(q, args).fetchall()
        # id를 함께 돌려준다 — 두 이벤트의 선후는 벽시계(ts)가 아니라 rowid로 판정해야
        # NTP 역행 같은 시계 이상에서도 순서가 뒤집히지 않는다
        return [
            {"id": r[0], "ts": r[1], "event_type": r[2], "task_id": r[3],
             "execution_id": r[4], "instance_id": r[5], "payload": json.loads(r[6])}
            for r in rows
        ]

    def upsert_instance(self, inst: AgentInstance) -> None:
        """projection 갱신 + 근거 이벤트를 함께 적재 — 이벤트가 진실."""
        self.append("InstanceEvent", task_id=inst.execution_id,
                    execution_id=inst.execution_id, instance_id=inst.instance_id,
                    payload=json.loads(inst.to_json()))
        self._con.execute(
            "INSERT INTO agent_instances (instance_id, body) VALUES (?, ?)"
            " ON CONFLICT(instance_id) DO UPDATE SET body = excluded.body",
            (inst.instance_id, inst.to_json()),
        )
        self._con.commit()

    def get_instance(self, instance_id: str) -> AgentInstance | None:
        row = self._con.execute(
            "SELECT body FROM agent_instances WHERE instance_id = ?", (instance_id,)
        ).fetchone()
        return AgentInstance.from_json(row[0]) if row else None

    def rebuild_instances(self) -> int:
        """events의 최신 InstanceEvent로 projection 재구축 — §13.1 재계산 가능성 증명."""
        latest: dict[str, dict] = {}
        for ev in self.events(event_type="InstanceEvent"):
            latest[ev["instance_id"]] = ev["payload"]
        self._con.execute("DELETE FROM agent_instances")
        for iid, body in latest.items():
            self._con.execute(
                "INSERT INTO agent_instances (instance_id, body) VALUES (?, ?)",
                (iid, json.dumps(body, ensure_ascii=False)),
            )
        self._con.commit()
        return len(latest)
