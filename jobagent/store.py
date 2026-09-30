"""SQLite state per profile: what was seen, how it was judged, what was emailed."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

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
        # The model's verdict per job title the profile's patterns don't cover (titles.py).
        self.db.execute("""CREATE TABLE IF NOT EXISTS title_verdicts (
            profile TEXT NOT NULL, title TEXT NOT NULL, fingerprint TEXT NOT NULL, fits INTEGER NOT NULL,
            decided_at TEXT NOT NULL, PRIMARY KEY (profile, title))""")
        # Companies found by discovery (companies.py) and where they post jobs: status pending | found | none.
        self.db.execute("""CREATE TABLE IF NOT EXISTS companies (
            key TEXT PRIMARY KEY, name TEXT NOT NULL, website TEXT, ats TEXT, slug TEXT, status TEXT NOT NULL,
            origin TEXT, first_seen TEXT NOT NULL, checked_at TEXT)""")
        # Per profile: is this company like the ones the person wants? origin: model | you (chat/CLI).
        self.db.execute("""CREATE TABLE IF NOT EXISTS company_verdicts (
            profile TEXT NOT NULL, key TEXT NOT NULL, name TEXT NOT NULL, fits INTEGER NOT NULL, origin TEXT NOT NULL,
            decided_at TEXT NOT NULL, PRIMARY KEY (profile, key))""")
        # 👍/👎 on jobs, from the email buttons (synced from the web app) or the command line / chat.
        self.db.execute("""CREATE TABLE IF NOT EXISTS votes (
            profile TEXT NOT NULL, key TEXT NOT NULL, title TEXT, company TEXT, vote TEXT NOT NULL, reason TEXT,
            at TEXT NOT NULL, origin TEXT NOT NULL, PRIMARY KEY (profile, key))""")
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

    def title_verdicts(self, profile, fingerprint) -> dict[str, bool]:
        rows = self.db.execute(
            "SELECT title, fits FROM title_verdicts WHERE profile=? AND fingerprint=?", (profile, fingerprint)
        )
        return {t: bool(f) for t, f in rows}

    def save_title_verdicts(self, profile, fingerprint, verdicts: dict[str, bool]):
        now = _now()
        self.db.executemany(
            "INSERT INTO title_verdicts (profile, title, fingerprint, fits, decided_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(profile, title) DO UPDATE SET fingerprint=excluded.fingerprint, fits=excluded.fits, "
            "decided_at=excluded.decided_at",
            [(profile, t, fingerprint, int(ok), now) for t, ok in verdicts.items()],
        )

    # ---------- companies (discovery) ----------

    _COMPANY = ("key", "name", "website", "ats", "slug", "status", "origin", "checked_at")

    def company(self, key) -> dict | None:
        row = self.db.execute(
            "SELECT key, name, website, ats, slug, status, origin, checked_at FROM companies WHERE key=?", (key,)
        ).fetchone()
        return dict(zip(self._COMPANY, row, strict=True)) if row else None

    def companies(self, status=None) -> list[dict]:
        sql = "SELECT key, name, website, ats, slug, status, origin, checked_at FROM companies"
        rows = self.db.execute(
            sql + (" WHERE status=?" if status else "") + " ORDER BY name", (status,) if status else ()
        )
        return [dict(zip(self._COMPANY, r, strict=True)) for r in rows]

    def add_company(self, key, name, website="", origin=""):
        """New companies start as 'pending' (not looked up yet); a known one only gains a missing website."""
        self.db.execute(
            "INSERT INTO companies (key, name, website, status, origin, first_seen) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET website=COALESCE(NULLIF(companies.website, ''), excluded.website)",
            (key, name, website or "", "pending", origin, _now()),
        )

    def set_company_source(self, key, status, ats="", slug=""):
        self.db.execute(
            "UPDATE companies SET status=?, ats=?, slug=?, checked_at=? WHERE key=?", (status, ats, slug, _now(), key)
        )

    def found_sources(self) -> dict[str, list[str]]:
        """{"greenhouse": [slug, …], "workday": [...]} for every company whose job board discovery found."""
        out: dict[str, list[str]] = {}
        for ats, slug in self.db.execute("SELECT ats, slug FROM companies WHERE status='found'"):
            out.setdefault(ats, []).append(slug)
        return out

    def company_verdicts(self, profile) -> dict[str, dict]:
        rows = self.db.execute("SELECT key, name, fits, origin FROM company_verdicts WHERE profile=?", (profile,))
        return {k: {"name": n, "fits": bool(f), "origin": o} for k, n, f, o in rows}

    def set_company_verdict(self, profile, key, name, fits, origin):
        """The person's own verdict ('you') is never overwritten by the model's."""
        self.db.execute(
            "INSERT INTO company_verdicts (profile, key, name, fits, origin, decided_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(profile, key) DO UPDATE SET fits=excluded.fits, origin=excluded.origin, "
            "decided_at=excluded.decided_at WHERE company_verdicts.origin != 'you' OR excluded.origin = 'you'",
            (profile, key, name, int(fits), origin, _now()),
        )

    def forget_company_verdict(self, profile, key):
        self.db.execute("DELETE FROM company_verdicts WHERE profile=? AND key=?", (profile, key))

    # ---------- votes ----------

    def set_vote(self, profile, key, title, company, vote, reason, origin, at=None):
        self.db.execute(
            "INSERT INTO votes (profile, key, title, company, vote, reason, at, origin) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(profile, key) DO UPDATE SET vote=excluded.vote, reason=excluded.reason, at=excluded.at, "
            "origin=excluded.origin",
            (profile, key, title, company, vote, reason or "", at or _now(), origin),
        )

    def votes(self, profile) -> list[dict]:
        rows = self.db.execute(
            "SELECT key, title, company, vote, reason, at, origin FROM votes WHERE profile=? ORDER BY at DESC",
            (profile,),
        )
        return [dict(zip(("key", "title", "company", "vote", "reason", "at", "origin"), r, strict=True)) for r in rows]

    def last_vote_at(self) -> str:
        return self.db.execute("SELECT COALESCE(MAX(at), '') FROM votes WHERE origin='email'").fetchone()[0]

    def emailed(self, profile, days=30) -> list[dict]:
        """Jobs sent to a profile in the last `days` days, newest first – what the person can vote on."""
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        rows = self.db.execute(
            "SELECT key, title, company, url, total, emailed_at FROM matches WHERE profile=? AND emailed_at >= ? "
            "ORDER BY emailed_at DESC",
            (profile, since),
        )
        return [dict(zip(("key", "title", "company", "url", "total", "emailed_at"), r, strict=True)) for r in rows]

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
