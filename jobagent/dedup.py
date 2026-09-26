"""Duplicate detection across sources, and job ids that stay stable across runs.

The same opening shows up on the employer's board and on several aggregators, under slightly different names
("ibm" / "International Business Machines", "Acme GmbH" / "ACME"), with cut-off titles ("… (US Canada, Europe, MENA, I")
or decorations ("(m/w/d)", "- 100% remote", "Entwickler:in"). Each copy costs matching time – and in the job agent a
language-model call – and nobody wants the same job twice.

`dedupe()` merges copies into one job (the employer's own posting wins) and gives it a stable `uid`. `AliasBook`
remembers which keys belong together, so a job keeps its id (and with it its stored assessment and "already sent"
state) even when the copy we saw first disappears. Everything is dictionary lookups; fuzzy comparisons only happen
within one company or one title, so it stays fast for hundreds of thousands of postings.
"""

import html
import re
import time
import zlib
from collections import Counter, defaultdict
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field

from .sources import Job

# ---------- normalisation ----------

_GENDER = re.compile(
    r"\(\s*(?:[mwfdxhi]|div|all genders?|alle geschlechter|gn\*?)(?:\s*[/|,]\s*(?:[mwfdxhi]|div|divers))*\s*\)"
    r"|\b[mwfhd]\s*/\s*[mwfhd]\s*(?:/\s*[dmwfx])?\b",
    re.I,
)
_GENDER_SUFFIX = re.compile(
    r"(?<=\w)(?:[:*_]|/-?)in(?:nen)?\b|\(in\)", re.I
)  # Entwickler:in, Berater/-in, Entwickler*innen
# Words that only say how / how much / which time zone, never what the job is – a title part made only of these is
# dropped. Countries and cities are NOT in here: "Engineer – Spain" and "Engineer – Germany" are two jobs.
_NOISE = frozenset(
    "remote remoto hybrid onsite on site vor ort homeoffice home office fully full part time vollzeit teilzeit "
    "anywhere worldwide global europe european emea eu eea dach apac latam mena amer americas timezones timezone "
    "time zones zone cet cest utc gmt deutschlandweit bundesweit wide nationwide ortsunabhängig and und or oder "
    "with mit percent m w d f x h".split()
)
_TIMEZONES = re.compile(r"\btime ?zones?\b|\bzeitzonen?\b", re.I)  # "(US Canada, Europe … APAC Timezones)"
_LEGAL = frozenset(
    "gmbh mbh ag se kg kgaa ug ohg gbr ev e v co inc incorporated ltd limited llc llp plc sl slu sa sas sarl srl spa "
    "bv nv oy oyj ab as asa aps corp corporation company holding holdings group gruppe the and und".split()
)
_WORD = re.compile(r"[a-z0-9à-ÿß+#]+")


def _tokens(s: str) -> list[str]:
    return _WORD.findall(html.unescape(s or "").lower())


def company_tokens(name: str) -> tuple[str, ...]:
    """'ACME Software GmbH & Co. KG' → ('acme', 'software'). Keeps the raw words if only legal forms are left."""
    words = _tokens(name)
    return tuple(w for w in words if w not in _LEGAL) or tuple(words)


def title_canon(title: str) -> str:
    """The job title without decorations: gender tags, gendered endings, and parts that only name a place, a time
    zone, a work mode or a percentage. A trailing unclosed parenthesis (a title cut off by an aggregator) is dropped."""
    t = html.unescape(title or "").split(" | ")[0]  # as Job.raw_key: never finer than the key the stores use
    t = _GENDER.sub(" ", t)
    t = _GENDER_SUFFIX.sub("", t)
    t = re.sub(r"\([^)]*$", " ", t)  # "… Contractor (US Canada, Europe, MENA, I"

    def keep(part: str) -> bool:
        words = [w for w in _tokens(part) if not w.isdigit()]
        return bool(words) and not all(w in _NOISE for w in words)

    t = re.sub(
        r"\(([^)]*)\)",
        lambda m: f" ({m.group(1)}) " if keep(m.group(1)) and not _TIMEZONES.search(m.group(1)) else " ",
        t,
    )
    parts = [p for p in re.split(r"\s+[-–—|/]\s+|\s*\|\s*|,\s+", t) if keep(p)]
    words = _tokens(" ".join(parts) if parts else t)
    return " ".join(words)


def _collapsed(tokens: tuple[str, ...]) -> str:
    return "".join(tokens)


# Extra words that don't make another company: "OLIVER" = "OLIVER Agency", "Grafana" = "Grafana Labs".
_CORP_EXTRA = frozenset(
    "agency software technologies technology tech labs lab digital consulting solutions systems services studio "
    "studios international global europe deutschland germany spain españa iberia uk usa io app hq".split()
)


def company_match(a: tuple[str, ...], b: tuple[str, ...]) -> str:
    """'strong' (same company), 'weak' (maybe – the descriptions must agree) or ''."""
    if not a or not b:
        return ""
    ca, cb = _collapsed(a), _collapsed(b)
    if ca == cb:
        return "strong"
    short, long = sorted((a, b), key=len)
    if long[: len(short)] == short and set(long[len(short) :]) <= _CORP_EXTRA:
        return "strong"
    if (
        len(short) == 1 and len(long) >= 2 and short[0] == "".join(w[0] for w in long)
    ):  # "ibm" = International Business …
        return "strong"
    if set(short) <= set(long) or (min(len(ca), len(cb)) >= 3 and (ca.startswith(cb) or cb.startswith(ca))):
        return "weak"  # "Hero" / "Delivery Hero", "tngtech" / "TNG Technology Consulting"
    return ""


# ---------- text similarity (bottom-k MinHash over word 5-grams) ----------

SIG_SIZE = 64
MIN_TEXT = 300  # shorter texts (search snippets) are too short to compare


def signature(text: str) -> tuple[int, ...]:
    words = _WORD.findall((text or "").lower())[:800]
    if len(" ".join(words)) < MIN_TEXT:
        return ()
    shingles = {zlib.crc32(" ".join(words[i : i + 5]).encode()) for i in range(max(1, len(words) - 4))}
    return tuple(sorted(shingles)[:SIG_SIZE])


def similarity(a: tuple[int, ...], b: tuple[int, ...]) -> float | None:
    """Estimated Jaccard similarity of two texts, or None if one of them is too short to tell."""
    if not a or not b:
        return None
    sa, sb = set(a), set(b)
    union = sorted(sa | sb)[:SIG_SIZE]
    return sum(1 for x in union if x in sa and x in sb) / len(union)


# ---------- merging ----------

# Fuzzy rules only join copies from DIFFERENT sources: one board never lists the same job twice under two titles, and
# companies reuse description boilerplate, so similar texts on one board are usually different jobs.
TITLE_JACCARD = 0.6  # similar titles at the same company, from another source …
TEXT_SAME = 0.9  # … are one job only if the descriptions are nearly identical
TEXT_WEAK = 0.6  # "weak" company-name match, or a cut-off title: the descriptions must agree
CUT_TITLE = 70  # aggregators cut titles at ~80 characters; a shorter title that is a prefix is a different job


@dataclass(eq=False)
class _Cluster:
    jobs: list[Job]
    company: tuple[str, ...]
    title: str
    match_keys: set[str] = field(default_factory=set)  # normalised company|title of every copy
    _sig: tuple[int, ...] | None = None

    @property
    def sig(self) -> tuple[int, ...]:
        if self._sig is None:
            self._sig = signature(max((j.text for j in self.jobs), key=len))
        return self._sig

    def other_source(self, job: Job) -> bool:
        return all(j.source != job.source for j in self.jobs)

    def best(self) -> Job:
        """The employer's own posting first, then the fullest description; gaps are filled from the other copies."""
        best = max(self.jobs, key=lambda j: (j.direct, not j.partner, len(j.text or ""), bool(j.salary_max)))
        for j in self.jobs:
            if j is best:
                continue
            if not (best.salary_min or best.salary_max) and (j.salary_min or j.salary_max):
                best.salary_min, best.salary_max, best.currency = j.salary_min, j.salary_max, j.currency
            best.employment_type = best.employment_type or j.employment_type
            best.remote = best.remote or j.remote
            best.posted = best.posted or j.posted
        best.copies = len(self.jobs)
        return best


@dataclass
class Deduper:
    """Collects jobs and merges copies of the same opening. Call `add()` for each job, then `result()`."""

    _clusters: list[_Cluster] = field(default_factory=list)
    _by_key: dict[str, _Cluster] = field(default_factory=dict)
    _by_title: dict[str, list[_Cluster]] = field(default_factory=lambda: defaultdict(list))
    _by_company: dict[str, list[_Cluster]] = field(default_factory=lambda: defaultdict(list))

    def add(self, job: Job):
        company, title = company_tokens(job.company), title_canon(job.title) or job.raw_key
        key = f"{_collapsed(company)}|{title}"
        c = self._by_key.get(key) or self._same_title(company, title, job) or self._same_company(company, title, job)
        if c:
            c.jobs.append(job)
            c.match_keys.add(key)
            c._sig = None  # recomputed from the longest description when needed
            self._by_key.setdefault(key, c)
            return
        c = _Cluster([job], company, title, {key})
        self._clusters.append(c)
        self._by_key[key] = c
        self._by_title[title].append(c)
        self._by_company[_collapsed(company)].append(c)

    def _same_title(self, company, title, job) -> _Cluster | None:
        for c in self._by_title.get(title, ()):
            kind = company_match(company, c.company)
            if kind == "strong":
                return c
            if kind == "weak" and c.other_source(job) and (similarity(signature(job.text), c.sig) or 0) >= TEXT_WEAK:
                return c
        return None

    def _same_company(self, company, title, job) -> _Cluster | None:
        words, sig = set(title.split()), None
        for c in self._by_company.get(_collapsed(company), ()):
            if not c.other_source(job):
                continue
            if sig is None:
                sig = signature(job.text)
            cut = min((job, *c.jobs), key=lambda j: len(title_canon(j.title)))
            if len(cut.title.strip()) >= CUT_TITLE and max(title, c.title, key=len).startswith(
                min(title, c.title, key=len)
            ):
                sim = similarity(sig, c.sig)  # an aggregator cut the title off
                if sim is None or sim >= TEXT_WEAK:
                    return c
                continue
            other = set(c.title.split())
            if (
                len(words & other) / max(1, len(words | other)) >= TITLE_JACCARD
                and (similarity(sig, c.sig) or 0) >= TEXT_SAME
            ):
                return c
        return None

    def result(self, book: "AliasBook | None" = None) -> list[Job]:
        """One job per opening. With a book, each gets the id its copies had in earlier runs (else a new one)."""
        clusters = [(c, c.best()) for c in self._clusters]
        # Besides every copy's own key, the normalised "company|title" (prefixed m:) is remembered too, so a copy
        # that turns up only later under a slightly different name ("ACME GmbH") still finds its id.
        known = book.lookup({k for c, _ in clusters for k in self._keys(c)}) if book else {}
        remember, used = {}, set()
        for c, best in clusters:
            keys = self._keys(c)
            ids = Counter(known[k] for k in keys if k in known)
            # Two openings never share an id – e.g. two jobs that once looked like one.
            choices = [u for u, _ in ids.most_common()] + [
                best.raw_key,
                f"{best.raw_key}#{zlib.crc32(best.url.encode())}",
            ]
            best.uid = next(u for u in choices if u not in used)
            used.add(best.uid)
            best.aliases = ({j.raw_key for j in c.jobs} | set(ids)) - {best.uid}
            remember.update(dict.fromkeys(keys, best.uid))
        if book:
            book.remember(remember)
        return [best for _, best in clusters]

    @staticmethod
    def _keys(c: _Cluster) -> set[str]:
        return {j.raw_key for j in c.jobs} | {"m:" + k for k in c.match_keys}


def dedupe(jobs: list[Job], book: "AliasBook | None" = None) -> list[Job]:
    d = Deduper()
    for j in jobs:
        d.add(j)
    return d.result(book)


# ---------- persistent aliases ----------

ALIAS_DDL = "CREATE TABLE IF NOT EXISTS job_alias (alias TEXT PRIMARY KEY, uid TEXT NOT NULL, seen INTEGER NOT NULL)"


class AliasBook:
    """Which job keys belong to which stable id, in a SQLite table (ALIAS_DDL) of the caller's database.

    `connect` returns a context manager that yields a sqlite3 connection (and commits, if the caller wants that).
    Entries not seen for `keep_days` are dropped, so the table only holds jobs that are still around."""

    CHUNK = 500

    def __init__(self, connect: Callable[[], AbstractContextManager], keep_days: int = 90, read_only: bool = False):
        self.connect, self.keep_days, self.read_only = connect, keep_days, read_only

    def lookup(self, keys: set[str]) -> dict[str, str]:
        keys, out = list(keys), {}
        with self.connect() as c:
            for i in range(0, len(keys), self.CHUNK):
                part = keys[i : i + self.CHUNK]
                q = f"SELECT alias, uid FROM job_alias WHERE alias IN ({','.join('?' * len(part))})"
                out.update(c.execute(q, part).fetchall())
        return out

    def remember(self, mapping: dict[str, str]):
        if self.read_only:
            return
        now = int(time.time())
        with self.connect() as c:
            c.executemany(
                "INSERT INTO job_alias (alias, uid, seen) VALUES (?,?,?) "
                "ON CONFLICT(alias) DO UPDATE SET uid=excluded.uid, seen=excluded.seen",
                [(k, v, now) for k, v in mapping.items()],
            )
            c.execute("DELETE FROM job_alias WHERE seen < ?", (now - self.keep_days * 86400,))


def create_alias_table(conn, existing_keys_sql: str):
    """Create the alias table. When it is new, every key already in the database becomes its own id, so nothing
    stored so far (assessments, sent jobs) loses its link. `existing_keys_sql` selects those keys as column `k`."""
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_alias'").fetchone()
    conn.execute(ALIAS_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_job_alias_seen ON job_alias(seen)")  # for pruning
    if exists:
        return
    conn.execute(
        "INSERT OR IGNORE INTO job_alias (alias, uid, seen) "
        f"SELECT k, k, CAST(strftime('%s','now') AS INTEGER) FROM ({existing_keys_sql})"
    )
