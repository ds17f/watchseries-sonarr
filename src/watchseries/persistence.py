"""SQLite-backed job persistence.

Stores Job records so the service can survive restarts. Progress percent
is in-memory only — writes happen on state transitions and when a file
completes, not on every ffmpeg progress tick.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .jobs import Episode, Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    hash TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    tmdb_id TEXT NOT NULL,
    title TEXT NOT NULL,
    year TEXT NOT NULL DEFAULT '',
    quality TEXT NOT NULL DEFAULT '1080p',
    save_path TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    episodes_json TEXT NOT NULL DEFAULT '[]',
    season_pack INTEGER,
    added_on REAL NOT NULL,
    state TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0.0,
    size_total INTEGER NOT NULL DEFAULT 0,
    size_done INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    files_json TEXT NOT NULL DEFAULT '[]'
);
"""


class JobStore:
    """Thread-safe SQLite wrapper for Job records."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # check_same_thread=False because workers in any thread call save().
        # We serialize with our own lock to keep things sane.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False,
                                     isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def save(self, job: Job) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO jobs
                   (hash, name, media_type, tmdb_id, title, year, quality,
                    save_path, category, episodes_json, season_pack,
                    added_on, state, progress, size_total, size_done,
                    error, files_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(hash) DO UPDATE SET
                     name=excluded.name,
                     state=excluded.state,
                     progress=excluded.progress,
                     size_total=excluded.size_total,
                     size_done=excluded.size_done,
                     error=excluded.error,
                     files_json=excluded.files_json,
                     episodes_json=excluded.episodes_json,
                     season_pack=excluded.season_pack""",
                (
                    job.hash, job.name, job.media_type, job.tmdb_id,
                    job.title, job.year, job.quality, str(job.save_path),
                    job.category,
                    json.dumps([[e.season, e.episode] for e in job.episodes]),
                    job.season_pack, job.added_on, job.state, job.progress,
                    job.size_total, job.size_done, job.error,
                    json.dumps([str(p) for p in job.files]),
                ),
            )

    def delete(self, hash_: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM jobs WHERE hash=?", (hash_,))

    def load_all(self) -> list[Job]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM jobs").fetchall()
            cols = [c[0] for c in self._conn.execute(
                "SELECT * FROM jobs LIMIT 0").description]
        out: list[Job] = []
        for row in rows:
            r = dict(zip(cols, row))
            out.append(Job(
                hash=r["hash"], name=r["name"],
                media_type=r["media_type"], tmdb_id=r["tmdb_id"],
                title=r["title"], year=r["year"], quality=r["quality"],
                save_path=Path(r["save_path"]), category=r["category"],
                episodes=[Episode(s, e) for s, e in
                          json.loads(r["episodes_json"] or "[]")],
                season_pack=r["season_pack"],
                added_on=r["added_on"], state=r["state"],
                progress=r["progress"],
                size_total=r["size_total"], size_done=r["size_done"],
                error=r["error"],
                files=[Path(p) for p in json.loads(r["files_json"] or "[]")],
            ))
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()
