"""SQLite storage for jobs and their event log. Small enough to read in one sitting."""
import json
import sqlite3
import time
import uuid
from pathlib import Path

JOB_FIELDS = [
    "id", "machine_id", "machine_name", "profile", "status", "state", "app", "progress_pct", "frame",
    "total_frames", "eta", "summary", "error_text", "check_interval_sec", "stall_minutes",
    "started_at", "ended_at", "last_check_at", "muted_until", "slack_ts", "connection_id",
]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS jobs ({", ".join(f + (" TEXT PRIMARY KEY" if f == "id" else "") for f in JOB_FIELDS)});
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, at REAL, kind TEXT, state TEXT, text TEXT
);
"""

# status: active (being watched), finished, stopped (by a person), failed (gave up)
ACTIVE = "active"


class Store:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir
        self.db = sqlite3.connect(data_dir / "render_buddy.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def job_dir(self, job_id: str) -> Path:
        return self.data_dir / "jobs" / job_id

    def create_job(self, **fields) -> dict:
        job = {f: None for f in JOB_FIELDS}
        job.update(fields, id=uuid.uuid4().hex[:12], status=ACTIVE, state="starting", started_at=time.time())
        self.db.execute(f"INSERT INTO jobs ({','.join(JOB_FIELDS)}) VALUES ({','.join('?' * len(JOB_FIELDS))})",
                        [job[f] for f in JOB_FIELDS])
        self.db.commit()
        return job

    def update_job(self, job_id: str, **fields) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE jobs SET {sets} WHERE id = ?", [*fields.values(), job_id])
        self.db.commit()

    def get_job(self, job_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM jobs WHERE id = ?", [job_id]).fetchone()
        return dict(row) if row else None

    def list_jobs(self, active: bool | None = None, limit: int = 100) -> list[dict]:
        where = "" if active is None else ("WHERE status = 'active'" if active else "WHERE status != 'active'")
        rows = self.db.execute(f"SELECT * FROM jobs {where} ORDER BY started_at DESC LIMIT ?", [limit])
        return [dict(r) for r in rows]

    def add_event(self, job_id: str, kind: str, state: str, text: str) -> dict:
        ev = {"job_id": job_id, "at": time.time(), "kind": kind, "state": state, "text": text}
        self.db.execute("INSERT INTO events (job_id, at, kind, state, text) VALUES (?, ?, ?, ?, ?)",
                        list(ev.values()))
        self.db.commit()
        return ev

    def events(self, job_id: str, limit: int = 50) -> list[dict]:
        rows = self.db.execute("SELECT * FROM events WHERE job_id = ? ORDER BY at DESC LIMIT ?", [job_id, limit])
        return [dict(r) for r in rows]


def to_json(obj) -> str:
    return json.dumps(obj, default=str)
