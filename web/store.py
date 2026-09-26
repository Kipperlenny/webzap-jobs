"""Encrypted SQLite storage. There are no accounts: every action is authorised by a random token sent by email.

Email and text are Fernet-encrypted. The email is additionally stored as an HMAC so we can find/replace a person's
entry without decrypting everything. Tokens (confirm, manage, delete) are stored only as SHA-256 hashes, so a
database leak does not leak working links.
"""

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager

from cryptography.fernet import Fernet

from settings import CONFIRM_TTL_HOURS, DB_PATH, ENCRYPTION_KEY, HASH_PEPPER

FREQUENCIES = ("daily", "weekly", "paused")
_fernet = Fernet(ENCRYPTION_KEY.encode())


_schema_ready = False
_schema_lock = threading.Lock()  # the web app serves requests from a thread pool


def _migrate(c: sqlite3.Connection):
    """Create/upgrade the schema. Runs once per process."""
    c.execute("PRAGMA journal_mode=WAL")  # web app and worker share this file
    c.execute("""CREATE TABLE IF NOT EXISTS signups (
        id INTEGER PRIMARY KEY,
        email_hash TEXT NOT NULL,
        email_enc BLOB NOT NULL,
        text_enc BLOB NOT NULL,
        confirm_hash TEXT UNIQUE,
        delete_hash TEXT UNIQUE NOT NULL,
        created_at INTEGER NOT NULL,
        confirmed_at INTEGER)""")
    cols = {r[1] for r in c.execute("PRAGMA table_info(signups)")}
    for col, ddl in [
        ("manage_hash", "TEXT"),
        ("frequency", "TEXT NOT NULL DEFAULT 'weekly'"),
        ("updated_at", "INTEGER"),
        ("derived_enc", "BLOB"),
        ("derived_at", "INTEGER"),
        ("derived_by", "TEXT"),
        # Manage token, encrypted, so later emails (digests) can include the manage link.
        ("manage_token_enc", "BLOB"),
        ("first_digest_at", "INTEGER"),
        ("last_digest_at", "INTEGER"),
        ("lang", "TEXT NOT NULL DEFAULT 'en'"),  # language of our emails (i18n.LANGS)
        # Ad campaign the person arrived from; kept only until confirmation, to count it (see campaign_stats).
        ("campaign", "TEXT"),
    ]:
        if col not in cols:
            c.execute(f"ALTER TABLE signups ADD COLUMN {col} {ddl}")
    c.execute("CREATE INDEX IF NOT EXISTS ix_email ON signups(email_hash)")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_manage ON signups(manage_hash)")
    # Durable task queue. The LLM server is not always online, so tasks wait here until it is.
    c.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY,
        signup_id INTEGER NOT NULL REFERENCES signups(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',      -- pending | running | done | failed
        attempts INTEGER NOT NULL DEFAULT 0,         -- failed model answers (offline time doesn't count)
        run_after INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_tasks_due ON tasks(status, run_after)")
    # Jobs sent to a subscriber (never sent twice) and their 👍/👎 feedback.
    c.execute("""CREATE TABLE IF NOT EXISTS sent_jobs (
        id INTEGER PRIMARY KEY,
        signup_id INTEGER NOT NULL REFERENCES signups(id) ON DELETE CASCADE,
        job_key TEXT NOT NULL, title TEXT, company TEXT, url TEXT, source TEXT, partner TEXT, sponsored_id TEXT,
        score INTEGER, reason TEXT, sent_at INTEGER NOT NULL,
        token_hash TEXT UNIQUE NOT NULL,
        feedback TEXT, feedback_reason TEXT, feedback_at INTEGER,
        UNIQUE (signup_id, job_key))""")
    # Ad campaign counts: plain numbers per day and campaign – no per-person data (see privacy policy).
    c.execute("""CREATE TABLE IF NOT EXISTS campaign_stats (
        day TEXT NOT NULL, campaign TEXT NOT NULL,
        visits INTEGER NOT NULL DEFAULT 0, signups INTEGER NOT NULL DEFAULT 0, confirmed INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, campaign))""")
    sent_cols = {r[1] for r in c.execute("PRAGMA table_info(sent_jobs)")}
    for col in ("matched_role", "location"):  # used to learn from feedback ("not my field", "wrong location")
        if col not in sent_cols:
            c.execute(f"ALTER TABLE sent_jobs ADD COLUMN {col} TEXT")


@contextmanager
def _conn():
    """A short-lived connection: commits on success, rolls back on error, always closes."""
    global _schema_ready
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    try:
        c.execute("PRAGMA foreign_keys=ON")
        if not _schema_ready:
            with _schema_lock:
                if not _schema_ready:
                    _migrate(c)
                    c.commit()
                    _schema_ready = True
        with c:
            yield c
    finally:
        c.close()


def email_hash(email: str) -> str:
    return hmac.new(HASH_PEPPER, email.strip().lower().encode(), hashlib.sha256).hexdigest()


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def purge_expired():
    cutoff = int(time.time()) - CONFIRM_TTL_HOURS * 3600
    with _conn() as c:
        c.execute("DELETE FROM signups WHERE confirmed_at IS NULL AND created_at < ?", (cutoff,))


def recent_pending(email: str, seconds: int) -> int:
    with _conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM signups WHERE email_hash=? AND confirmed_at IS NULL AND created_at > ?",
            (email_hash(email), int(time.time()) - seconds),
        ).fetchone()[0]


def add_pending(email: str, text: str, frequency: str, lang: str = "en", campaign: str = "") -> dict:
    """Store an unconfirmed signup; returns its fresh tokens {confirm, manage, delete}."""
    t = {k: secrets.token_urlsafe(32) for k in ("confirm", "manage", "delete")}
    with _conn() as c:
        c.execute(
            """INSERT INTO signups (email_hash, email_enc, text_enc, confirm_hash, manage_hash, manage_token_enc,
                     delete_hash, frequency, lang, campaign, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                email_hash(email),
                _fernet.encrypt(email.strip().encode()),
                _fernet.encrypt(text.encode()),
                _h(t["confirm"]),
                _h(t["manage"]),
                _fernet.encrypt(t["manage"].encode()),
                _h(t["delete"]),
                frequency,
                lang,
                campaign or None,
                int(time.time()),
            ),
        )
        if campaign:
            _count(c, campaign, "signups")
    return t


def _enqueue(c, signup_id: int, kind: str = "extract"):
    """Queue a task, replacing any not-yet-finished task of the same kind for this signup."""
    t = int(time.time())
    c.execute("DELETE FROM tasks WHERE signup_id=? AND kind=? AND status IN ('pending','failed')", (signup_id, kind))
    c.execute("INSERT INTO tasks (signup_id, kind, created_at, updated_at) VALUES (?,?,?,?)", (signup_id, kind, t, t))


def confirm(token: str) -> dict | None:
    """Activate a pending signup (it replaces any earlier entry for the same email).

    Returns what the welcome email needs – {email, lang, manage} – or None for an invalid token."""
    with _conn() as c:
        row = c.execute(
            """SELECT id, email_hash, email_enc, lang, manage_token_enc, campaign FROM signups
               WHERE confirm_hash=? AND confirmed_at IS NULL""",
            (_h(token),),
        ).fetchone()
        if not row:
            return None
        c.execute("DELETE FROM signups WHERE email_hash=? AND id != ?", (row[1], row[0]))
        c.execute(
            "UPDATE signups SET confirmed_at=?, confirm_hash=NULL, campaign=NULL WHERE id=?",
            (int(time.time()), row[0]),
        )
        if row[5]:
            _count(c, row[5], "confirmed")
        _enqueue(c, row[0])
        return {"email": _fernet.decrypt(row[2]).decode(), "lang": row[3], "manage": _fernet.decrypt(row[4]).decode()}


def token_valid(kind: str, token: str) -> bool:
    col = {"confirm": "confirm_hash", "delete": "delete_hash"}[kind]
    with _conn() as c:
        return c.execute(f"SELECT 1 FROM signups WHERE {col}=?", (_h(token),)).fetchone() is not None


def get_by_manage(token: str) -> dict | None:
    """Everything we store about the person holding this manage token, decrypted."""
    with _conn() as c:
        row = c.execute(
            """SELECT id, email_enc, text_enc, frequency, created_at, confirmed_at, updated_at,
                                  derived_enc, derived_at, derived_by, lang FROM signups WHERE manage_hash=?""",
            (_h(token),),
        ).fetchone()
        if not row:
            return None
        task = c.execute(
            """SELECT status, attempts FROM tasks WHERE signup_id=? AND kind='extract'
                            ORDER BY id DESC LIMIT 1""",
            (row[0],),
        ).fetchone()
    return {
        "email": _fernet.decrypt(row[1]).decode(),
        "text": _fernet.decrypt(row[2]).decode(),
        "frequency": row[3],
        "lang": row[10],
        "created_at": row[4],
        "confirmed_at": row[5],
        "updated_at": row[6],
        # Search criteria our language model derived from the text (see extract.py).
        "derived": json.loads(_fernet.decrypt(row[7])) if row[7] else None,
        "derived_at": row[8],
        "derived_by": row[9],
        # "…:fb7" = the last analysis also used feedback on 7 jobs
        "derived_feedback": int(row[9].rsplit(":fb", 1)[1]) if row[9] and ":fb" in row[9] else 0,
        "derived_status": task[0] if task else None,
        "jobs_sent": feedback_summary(row[0]),
    }


def update(token: str, text: str, frequency: str, lang: str | None = None) -> bool:
    """Save an edit from the manage page; lang=None keeps the stored language."""
    with _conn() as c:
        row = c.execute(
            "SELECT id, text_enc, confirmed_at, lang FROM signups WHERE manage_hash=?", (_h(token),)
        ).fetchone()
        if not row:
            return False
        lang = lang or row[3]
        c.execute(
            "UPDATE signups SET text_enc=?, frequency=?, lang=?, updated_at=? WHERE id=?",
            (_fernet.encrypt(text.encode()), frequency, lang, int(time.time()), row[0]),
        )
        # Text or language changed → derive again (the summary and questions are written in the person's language).
        if row[2] and (_fernet.decrypt(row[1]).decode() != text or row[3] != lang):
            _enqueue(c, row[0])
        return True


def new_manage_token(email: str) -> str | None:
    """Rotate the manage token of a confirmed entry (lost-link flow). Old manage links stop working."""
    token = secrets.token_urlsafe(32)
    with _conn() as c:
        cur = c.execute(
            """UPDATE signups SET manage_hash=?, manage_token_enc=?
                           WHERE email_hash=? AND confirmed_at IS NOT NULL""",
            (_h(token), _fernet.encrypt(token.encode()), email_hash(email)),
        )
        return token if cur.rowcount else None


def _delete_where(col: str, token: str) -> bool:
    with _conn() as c:
        row = c.execute(f"SELECT email_hash FROM signups WHERE {col}=?", (_h(token),)).fetchone()
        if not row:
            return False
        c.execute("DELETE FROM signups WHERE email_hash=?", (row[0],))
        return True


def delete(token: str) -> bool:
    """Delete every entry belonging to the email address the delete token was issued for."""
    return _delete_where("delete_hash", token)


def delete_by_manage(token: str) -> bool:
    return _delete_where("manage_hash", token)


# ---------- worker side ----------


def reset_running():
    """After a crash/restart, tasks left 'running' go back to the queue."""
    with _conn() as c:
        c.execute("UPDATE tasks SET status='pending', updated_at=? WHERE status='running'", (int(time.time()),))


def pending_count() -> int:
    with _conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='pending' AND run_after<=?", (int(time.time()),)
        ).fetchone()[0]


def claim_task() -> dict | None:
    """Atomically take the oldest due task and return it with the decrypted text."""
    t = int(time.time())
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            """SELECT tasks.id, tasks.kind, tasks.attempts, signups.id, signups.text_enc, signups.lang FROM tasks
                           JOIN signups ON signups.id = tasks.signup_id
                           WHERE tasks.status='pending' AND tasks.run_after<=? ORDER BY tasks.id LIMIT 1""",
            (t,),
        ).fetchone()
        if not row:
            return None
        c.execute("UPDATE tasks SET status='running', updated_at=? WHERE id=?", (t, row[0]))
    return {
        "id": row[0],
        "kind": row[1],
        "attempts": row[2],
        "signup_id": row[3],
        "text": _fernet.decrypt(row[4]).decode(),
        "lang": row[5],
        "feedback": feedback_for_prompt(row[3]),
    }


def finish_task(task: dict, derived: dict, derived_by: str):
    t = int(time.time())
    with _conn() as c:
        c.execute(
            "UPDATE signups SET derived_enc=?, derived_at=?, derived_by=? WHERE id=?",
            (_fernet.encrypt(json.dumps(derived).encode()), t, derived_by, task["signup_id"]),
        )
        c.execute("UPDATE tasks SET status='done', last_error=NULL, updated_at=? WHERE id=?", (t, task["id"]))


def requeue_task(task: dict, delay: float, error: str = "", count_attempt: bool = False, max_attempts: int = 5):
    """Put a task back. Offline → no attempt counted. Bad model output → attempt counted, failed after max_attempts."""
    t = int(time.time())
    attempts = task["attempts"] + (1 if count_attempt else 0)
    status = "failed" if attempts >= max_attempts else "pending"
    with _conn() as c:
        c.execute(
            "UPDATE tasks SET status=?, attempts=?, run_after=?, last_error=?, updated_at=? WHERE id=?",
            (status, attempts, t + int(delay), error[:500], t, task["id"]),
        )


def backfill(prompt_version: int) -> int:
    """Queue extraction for confirmed entries without derived data from the current prompt version and no open task."""
    t = int(time.time())
    with _conn() as c:
        rows = c.execute(
            """SELECT id FROM signups WHERE confirmed_at IS NOT NULL
                            AND (derived_by IS NULL OR (derived_by NOT LIKE ? AND derived_by NOT LIKE ?))
                            AND id NOT IN (SELECT signup_id FROM tasks WHERE kind='extract'
                                           AND status IN ('pending','running'))""",
            (f"%:v{prompt_version}", f"%:v{prompt_version}:%"),
        ).fetchall()
        c.executemany(
            "INSERT INTO tasks (signup_id, kind, created_at, updated_at) VALUES (?, 'extract', ?, ?)",
            [(r[0], t, t) for r in rows],
        )
    return len(rows)


# ---------- digest side ----------


def manage_token_for(signup_id: int) -> str:
    """The manage token to put into an email. Entries from before tokens were stored encrypted get a fresh one."""
    with _conn() as c:
        row = c.execute("SELECT manage_token_enc FROM signups WHERE id=?", (signup_id,)).fetchone()
        if row and row[0]:
            return _fernet.decrypt(row[0]).decode()
        token = secrets.token_urlsafe(32)
        c.execute(
            "UPDATE signups SET manage_hash=?, manage_token_enc=? WHERE id=?",
            (_h(token), _fernet.encrypt(token.encode()), signup_id),
        )
        return token


def recipients() -> list[dict]:
    """Confirmed, not paused entries with their decrypted email and derived criteria (for the digest sender)."""
    with _conn() as c:
        rows = c.execute("""SELECT id, email_enc, frequency, derived_enc, first_digest_at, last_digest_at, lang
                            FROM signups WHERE confirmed_at IS NOT NULL AND frequency != 'paused'""").fetchall()
    return [
        {
            "id": r[0],
            "email": _fernet.decrypt(r[1]).decode(),
            "frequency": r[2],
            "derived": json.loads(_fernet.decrypt(r[3])) if r[3] else None,
            "first_digest_at": r[4],
            "last_digest_at": r[5],
            "lang": r[6],
        }
        for r in rows
    ]


def mark_digest_sent(signup_id: int):
    t = int(time.time())
    with _conn() as c:
        c.execute(
            "UPDATE signups SET first_digest_at=COALESCE(first_digest_at, ?), last_digest_at=? WHERE id=?",
            (t, t, signup_id),
        )


def sent_keys(signup_id: int) -> set[str]:
    with _conn() as c:
        return {r[0] for r in c.execute("SELECT job_key FROM sent_jobs WHERE signup_id=?", (signup_id,))}


FEEDBACK_REEXTRACT = 5  # new votes after which the text is re-analysed together with the feedback


def feedback_signals(signup_id: int) -> dict:
    """Condense a person's 👍/👎 votes into matching adjustments (keyword arguments for matching.Signals)."""
    with _conn() as c:
        rows = c.execute(
            """SELECT feedback, feedback_reason, company, matched_role, title, location FROM sent_jobs
               WHERE signup_id=? AND feedback IS NOT NULL""",
            (signup_id,),
        ).fetchall()
    sig = {
        "excluded_companies": set(),
        "role_up": {},
        "role_down": {},
        "liked_words": set(),
        "too_junior": False,
        "too_senior": False,
        "bad_places": set(),
        "salary_strict": False,
    }
    for vote, reason, company, role, title, location in rows:
        reason = reason or ""
        if vote == "up":
            if role:
                sig["role_up"][role] = sig["role_up"].get(role, 0) + 1
            sig["liked_words"] |= {w for w in re.findall(r"[a-zà-ÿ0-9+#]+", (title or "").lower()) if len(w) > 2}
            continue
        if "not this company" in reason and company:
            sig["excluded_companies"].add(company.lower())
        if "not my field" in reason and role:
            sig["role_down"][role] = sig["role_down"].get(role, 0) + 1
        sig["too_junior"] |= "too junior" in reason
        sig["too_senior"] |= "too senior" in reason
        sig["salary_strict"] |= "salary too low" in reason
        if "wrong location" in reason and location:
            sig["bad_places"].add(re.split(r"[,;/(|]", location.lower())[0].strip())
    return sig


def feedback_for_prompt(signup_id: int, limit: int = 30) -> list[dict]:
    """Recent votes, for re-analysing the text with the language model."""
    with _conn() as c:
        rows = c.execute(
            """SELECT title, company, feedback, feedback_reason FROM sent_jobs
               WHERE signup_id=? AND feedback IS NOT NULL ORDER BY feedback_at DESC LIMIT ?""",
            (signup_id, limit),
        ).fetchall()
    return [{"title": r[0], "company": r[1], "vote": r[2], "reasons": r[3] or ""} for r in rows]


def record_sent(signup_id: int, job, match, sponsored_id: str = "") -> str:
    """Remember a job as sent (with the role that matched, for learning); returns its feedback token."""
    token = secrets.token_urlsafe(24)
    with _conn() as c:
        c.execute(
            """INSERT OR IGNORE INTO sent_jobs (signup_id, job_key, title, company, url, source, partner, location,
                     sponsored_id, score, reason, matched_role, sent_at, token_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                signup_id,
                job.key,
                job.title,
                job.company,
                job.url,
                job.source,
                job.partner,
                job.location,
                sponsored_id or None,
                match.score,
                match.why,
                match.role,
                int(time.time()),
                _h(token),
            ),
        )
    return token


def feedback_get(token: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT title, company, url, feedback, feedback_reason FROM sent_jobs WHERE token_hash=?", (_h(token),)
        ).fetchone()
    return dict(zip(("title", "company", "url", "feedback", "feedback_reason"), row, strict=False)) if row else None


def feedback_set(token: str, vote: str, reason: str = "") -> bool:
    """Store a vote. After FEEDBACK_REEXTRACT new votes the text is re-analysed together with the feedback."""
    now = int(time.time())
    with _conn() as c:
        row = c.execute("SELECT signup_id FROM sent_jobs WHERE token_hash=?", (_h(token),)).fetchone()
        if not row:
            return False
        c.execute(
            "UPDATE sent_jobs SET feedback=?, feedback_reason=?, feedback_at=? WHERE token_hash=?",
            (vote, reason[:200], now, _h(token)),
        )
        new_votes = c.execute(
            """SELECT COUNT(*) FROM sent_jobs WHERE signup_id=? AND feedback_at >=
                   COALESCE((SELECT derived_at FROM signups WHERE id=?), 0)""",
            (row[0], row[0]),
        ).fetchone()[0]
        open_task = c.execute(
            "SELECT 1 FROM tasks WHERE signup_id=? AND kind='extract' AND status IN ('pending','running')", (row[0],)
        ).fetchone()
        if new_votes >= FEEDBACK_REEXTRACT and not open_task:
            _enqueue(c, row[0])
    return True


def feedback_summary(signup_id: int) -> dict:
    with _conn() as c:
        rows = c.execute(
            """SELECT title, company, feedback, feedback_reason, sent_at FROM sent_jobs
                            WHERE signup_id=? ORDER BY sent_at DESC""",
            (signup_id,),
        ).fetchall()
    return {
        "sent": len(rows),
        "up": sum(1 for r in rows if r[2] == "up"),
        "down": sum(1 for r in rows if r[2] == "down"),
        "jobs": [{"title": r[0], "company": r[1], "feedback": r[2], "reason": r[3], "sent_at": r[4]} for r in rows],
    }


def signup_id_by_manage(token: str) -> int | None:
    with _conn() as c:
        row = c.execute("SELECT id FROM signups WHERE manage_hash=?", (_h(token),)).fetchone()
    return row[0] if row else None


def unsubscribe(token: str) -> bool:
    """One-click unsubscribe: stop all emails (frequency 'paused'); data stays until the person deletes it."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE signups SET frequency='paused', updated_at=? WHERE manage_hash=?", (int(time.time()), _h(token))
        )
        return cur.rowcount == 1


def sponsored_stats() -> dict:
    """Aggregate numbers per sponsored job – the only thing a sponsor ever gets from us."""
    with _conn() as c:
        return {
            r[0]: {"sent": r[1], "up": r[2], "down": r[3]}
            for r in c.execute(
                """SELECT sponsored_id, COUNT(*), SUM(feedback='up'), SUM(feedback='down') FROM sent_jobs
               WHERE sponsored_id IS NOT NULL GROUP BY sponsored_id"""
            )
        }


# ---------- ad campaign counts ----------

CAMPAIGN_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
MAX_CAMPAIGNS = 200  # distinct names ever counted – made-up ?c= values can't grow the table without bound


def campaign_name(raw: str | None) -> str:
    """A valid campaign name from an ad link's ?c=, or ''."""
    name = (raw or "").strip().lower()
    return name if CAMPAIGN_RE.fullmatch(name) else ""


def _count(c, campaign: str, event: str):
    if event not in ("visits", "signups", "confirmed"):
        raise ValueError(event)
    known = c.execute("SELECT 1 FROM campaign_stats WHERE campaign=? LIMIT 1", (campaign,)).fetchone()
    if not known and c.execute("SELECT COUNT(DISTINCT campaign) FROM campaign_stats").fetchone()[0] >= MAX_CAMPAIGNS:
        return
    day = time.strftime("%Y-%m-%d", time.gmtime())
    c.execute("INSERT OR IGNORE INTO campaign_stats (day, campaign) VALUES (?, ?)", (day, campaign))
    c.execute(f"UPDATE campaign_stats SET {event}={event}+1 WHERE day=? AND campaign=?", (day, campaign))


def count_visit(campaign: str):
    with _conn() as c:
        _count(c, campaign, "visits")


def campaign_report(days: int = 30) -> list[tuple]:
    """(campaign, visits, signups, confirmed) summed over the last `days` days, most visits first."""
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    with _conn() as c:
        return c.execute(
            """SELECT campaign, SUM(visits), SUM(signups), SUM(confirmed) FROM campaign_stats WHERE day >= ?
               GROUP BY campaign ORDER BY SUM(visits) DESC""",
            (since,),
        ).fetchall()
