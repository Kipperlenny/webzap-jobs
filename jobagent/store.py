"""SQLite state per profile: what was seen, how it was judged, what was emailed."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

from .config import DATA_DIR, DB_PATH
from .dedup import AliasBook, create_alias_table


def _now():
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path=DB_PATH):
        DATA_DIR.mkdir(exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS matches (
            profile TEXT NOT NULL, key TEXT NOT NULL, url TEXT, company TEXT, title TEXT, source TEXT,
            first_seen TEXT, last_seen TEXT, status TEXT, reason TEXT, total INTEGER, assessment TEXT,
            emailed_at TEXT, PRIMARY KEY (profile, key))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS runs (
            profile TEXT, started TEXT, finished TEXT, scanned INTEGER, candidates INTEGER, assessed INTEGER,
            sent INTEGER, note TEXT)""")
        # External links shown in a digest (profile [[external]]), so each comes back only after repeat_days.
        self.db.execute("""CREATE TABLE IF NOT EXISTS external_shown (
            profile TEXT NOT NULL, url TEXT NOT NULL, shown_at TEXT NOT NULL, PRIMARY KEY (profile, url))""")
        create_alias_table(self.db, "SELECT DISTINCT key AS k FROM matches")
        self.db.commit()

    @contextmanager
    def _same_connection(self):
        yield self.db
        self.db.commit()

    def aliases(self) -> AliasBook:
        return AliasBook(self._same_connection)

    def get(self, profile, keys):
        """The stored state of a job, looked up by its id or – for jobs stored under another copy's key – an alias."""
        keys = [keys] if isinstance(keys, str) else list(keys)
        row = None
        for key in keys:
            row = self.db.execute(
                "SELECT status, reason, assessment, emailed_at FROM matches WHERE profile=? AND key=?", (profile, key)
            ).fetchone()
            if row:
                break
        if not row:
            return None
        return {
            "status": row[0],
            "reason": row[1],
            "assessment": json.loads(row[2]) if row[2] else None,
            "emailed_at": row[3],
        }

    def upsert(self, profile, job, status, reason, assessment=None):
        now = _now()
        self.db.execute(
            """INSERT INTO matches (profile,key,url,company,title,source,first_seen,last_seen,status,reason,
                           total,assessment) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(profile,key) DO UPDATE SET last_seen=excluded.last_seen, url=excluded.url,
              status=excluded.status, reason=excluded.reason, total=excluded.total,
              assessment=COALESCE(excluded.assessment, matches.assessment)""",
            (
                profile,
                job.key,
                job.url,
                job.company,
                job.title,
                job.source,
                now,
                now,
                status,
                reason,
                (assessment or {}).get("total"),
                json.dumps(assessment) if assessment else None,
            ),
        )

    def mark_emailed(self, profile, keys):
        now = _now()
        self.db.executemany(
            "UPDATE matches SET emailed_at=? WHERE profile=? AND key=?", [(now, profile, k) for k in keys]
        )

    def externals_shown(self, profile) -> dict[str, str]:
        """url -> when it was last shown."""
        return dict(self.db.execute("SELECT url, shown_at FROM external_shown WHERE profile=?", (profile,)))

    def mark_externals_shown(self, profile, urls):
        now = _now()
        self.db.executemany(
            "INSERT INTO external_shown (profile, url, shown_at) VALUES (?,?,?) "
            "ON CONFLICT(profile, url) DO UPDATE SET shown_at=excluded.shown_at",
            [(profile, u, now) for u in urls],
        )

    def log_run(self, profile, started, **kw):
        self.db.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)",
            (
                profile,
                started,
                _now(),
                kw.get("scanned"),
                kw.get("candidates"),
                kw.get("assessed"),
                kw.get("sent"),
                kw.get("note"),
            ),
        )

    def commit(self):
        self.db.commit()
