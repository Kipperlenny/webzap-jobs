"""Rule-based matching of job postings against a subscriber's derived search fields (see extract.py).

Runs on our server for every subscriber, so it has to be fast and cheap – no language model here.
`score()` returns a `Match` (score 0-100, human-readable reason, matched role) or None when a hard rule rules the job
out. Personal 👍/👎 feedback (`Signals`) adjusts the result.

Deliberately strict: an email with one excellent job beats one with ten "maybe"s – people stop reading the rest.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import NamedTuple

from jobagent.common import REMOTE, to_eur

STOP = frozenset("and or of the for in a an to with m w d f x h remote de y".split())
MODIFIERS = frozenset("senior sr junior jr principal staff associate assistant".split())  # optional in titles
# Words that say nothing about topical fit – they must not count as "the posting mentions what you care about".
GENERIC = frozenset(
    "remote hybrid onsite europe emea eu international global worldwide hiring team teams leadership management "
    "growth technology flexible startup start-up".split()
)
JUNIOR = re.compile(
    r"\b(intern|internship|trainee|werkstudent|working student|junior|graduate|apprentice|praktik)", re.I
)
SENIOR_TITLE = re.compile(r"\b(director|head|vp|vice president|chief|cto|cio|ceo|cfo|cmo)\b", re.I)
# Levels a person can tell us are wrong for them ("too junior" / "too senior").
LOWER_LEVEL = re.compile(r"\b(junior|jr|associate|assistant|coordinator|entry|trainee|intern)\b", re.I)
HIGHER_LEVEL = re.compile(r"\b(director|head|vp|vice president|chief|principal|executive)\b", re.I)
NOT_A_JOB = re.compile(
    r"\b(talent pool|talent community|future opportunit|open application|spontaneous application|initiativbewerbung"
    r"|general application|expression of interest)",
    re.I,
)
ROLE_MIN = 0.75  # share of a wanted role's words (incl. its key word) that must be in the title
FULL_TEXT = 600  # shorter texts are search snippets (partner feeds) – too short to demand keyword overlap


class Match(NamedTuple):
    score: int
    why: str  # English summary of the parts below, for logs and the database
    role: str
    loc_kind: str = ""  # 'local' | 'remote' | 'any'
    where: str = ""  # the place, or the remote region ('worldwide' included)
    hits: tuple[str, ...] = ()  # keywords the posting mentions


@dataclass
class Signals:
    """What a person's 👍/👎 feedback tells us (see store.feedback_signals)."""

    excluded_companies: set[str] = field(default_factory=set)
    role_up: dict[str, int] = field(default_factory=dict)  # role -> 👍 count
    role_down: dict[str, int] = field(default_factory=dict)  # role -> "not my field" count
    liked_words: set[str] = field(default_factory=set)  # title words of liked jobs
    too_junior: bool = False
    too_senior: bool = False
    bad_places: set[str] = field(default_factory=set)  # first location segment of "wrong location" jobs
    salary_strict: bool = False

    def blocked(self, role: str) -> bool:
        """A role is dropped after two 'not my field' votes that no 👍 outweighs."""
        return self.role_down.get(role, 0) >= 2 and self.role_up.get(role, 0) < self.role_down.get(role, 0)

    def role_bonus(self, role: str) -> float:
        return min(0.15, 0.05 * self.role_up.get(role, 0)) - 0.1 * self.role_down.get(role, 0)


NO_SIGNALS = Signals()


def words(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-zà-ÿ0-9+#]+", s.lower()) if w not in STOP and len(w) > 1]


def _has(text: str, phrase: str) -> bool:
    phrase = phrase.strip().lower()
    return bool(phrase) and re.search(r"\b" + re.escape(phrase) + r"\b", text) is not None


def first_place(location: str) -> str:
    """'Barcelona, Catalonia, Spain' → 'barcelona' – what a 'wrong location' vote refers to."""
    return re.split(r"[,;/(|]", (location or "").lower())[0].strip()


def _head(role: str, role_words: list[str]) -> str:
    """The word that makes the role: 'Head of Engineering' → head, 'Marketing Manager' → manager."""
    m = re.match(r"\s*([a-zà-ÿ]+)\s+of\b", role.lower())
    return m.group(1) if m and m.group(1) in role_words else role_words[-1]


def role_match(title: str, roles: list[str]) -> tuple[float, str]:
    """Best match of the job title against the wanted roles: 1.0 = all words of a role in the title."""
    t = set(words(title))
    best, best_role = 0.0, ""
    for i, role in enumerate(roles):
        role_words = [w for w in words(role) if w not in MODIFIERS]
        if not role_words:
            continue
        hit = sum(1 for w in role_words if w in t) / len(role_words)
        s = (hit if _head(role, role_words) in t else hit * 0.4) - 0.02 * i  # earlier roles are more wanted
        if s > best:
            best, best_role = s, role
    return best, best_role


class TitleIndex:
    """The job pool by title word, built once per run. A job can only match a role whose key word (`_head`) is in
    its title (see role_match and ROLE_MIN), so looking jobs up by those words finds exactly the jobs worth scoring –
    instead of scoring every subscriber against every job."""

    def __init__(self, jobs: list):
        self.jobs = jobs
        self._by_word: dict[str, list[int]] = defaultdict(list)
        for i, job in enumerate(jobs):
            for w in set(words(job.title)):
                self._by_word[w].append(i)

    def candidates(self, roles: list[str]) -> list:
        ids: set[int] = set()
        for role in roles:
            role_words = [w for w in words(role) if w not in MODIFIERS]
            if role_words:
                ids.update(self._by_word.get(_head(role, role_words), ()))
        return [self.jobs[i] for i in sorted(ids)]


def location_match(job, d: dict) -> tuple[str, str]:
    """('local'|'remote'|'any'|'', place or remote region). '' = incompatible."""
    loc = (job.location or "").lower()
    places = [p.get(k, "") for p in d.get("locations", []) for k in ("place", "country") if p.get(k)]
    regions = [r for r in d.get("remote_regions", []) if r]
    modes = set(d.get("work_modes", []))
    is_remote = job.remote or bool(REMOTE.search(loc)) or bool(REMOTE.search(job.title))
    if modes == {"remote"} and not is_remote:
        return "", ""
    for p in places:
        if _has(loc, p):
            return "local", p
    if is_remote and (regions or "remote" in modes or not places):
        global_ok = any(r.lower() in ("worldwide", "anywhere", "global", "international") for r in regions)
        for r in regions:
            if _has(loc, r) or _has(loc, {"europe": "eu"}.get(r.lower(), r)):
                return "remote", r
        if not loc.strip(" ,;") or re.fullmatch(r"\W*(remote|anywhere|worldwide|fully remote)\W*", loc):
            # A bare "Remote" often means "remote within the employer's country" (usually the US): accept it only
            # for people open to worldwide, or when the posting itself names one of their regions.
            if global_ok:
                return "remote", "worldwide"
            text = (job.text or "").lower()
            named = next((r for r in regions if _has(text, r) or _has(text, {"europe": "eu"}.get(r.lower(), r))), None)
            return ("remote", named) if named else ("", "")
        if global_ok and re.search(r"\b(worldwide|anywhere|global)\b", loc):
            return "remote", "worldwide"
    if not places and not regions and not modes:
        return "any", ""
    return "", ""


def salary_ok(job, d: dict) -> bool:
    s = d.get("salary") or {}
    floor, top = s.get("minimum"), to_eur(job.salary_max or job.salary_min, job.currency)
    if not floor or top is None or (s.get("period") or "year") != "year":
        return True
    return top < 1000 or top >= float(floor) * 0.9


def _excluded(job, d: dict, sig: Signals, title_l: str, company_l: str, text_l: str) -> bool:
    """Hard rules: anything here rules the job out completely."""
    if company_l and company_l in sig.excluded_companies:
        return True
    if NOT_A_JOB.search(job.title) or NOT_A_JOB.search(text_l[:1500]):
        return True
    seniority = set(d.get("seniority", []))
    if JUNIOR.search(job.title) and not seniority & {"entry", "junior", "any"}:
        return True
    if SENIOR_TITLE.search(job.title) and seniority and seniority <= {"entry", "junior"}:
        return True
    if (sig.too_junior and LOWER_LEVEL.search(job.title)) or (sig.too_senior and HIGHER_LEVEL.search(job.title)):
        return True
    if sig.bad_places and first_place(job.location) in sig.bad_places and not job.remote:
        return True
    for phrase in d.get("deal_breakers", []) + d.get("industries_excluded", []):
        # Multi-word deal breakers are specific enough to exclude on the description too ("agency jobs").
        if _has(title_l, phrase) or _has(company_l, phrase) or (len(phrase.split()) > 1 and _has(text_l, phrase)):
            return True
    return not salary_ok(job, d)


def score(job, d: dict, sig: Signals = NO_SIGNALS) -> Match | None:
    if not d:
        return None
    title_l, company_l, text_l = job.title.lower(), (job.company or "").lower(), (job.text or "").lower()[:6000]
    if _excluded(job, d, sig, title_l, company_l, text_l):
        return None

    roles = [r for r in d.get("target_roles", []) if not sig.blocked(r)]
    rs, role = role_match(job.title, roles)
    if rs < ROLE_MIN:
        return None
    rs += sig.role_bonus(role)
    loc_kind, where = location_match(job, d)
    if not loc_kind or (loc_kind == "any" and not (job.remote or REMOTE.search(job.location or ""))):
        return None

    places = {p.get(k, "").lower() for p in d.get("locations", []) for k in ("place", "country")}
    places |= {r.lower() for r in d.get("remote_regions", [])}
    wanted = [
        k
        for k in d.get("keywords", []) + d.get("skills", []) + d.get("industries_preferred", [])
        if len(k) > 2 and k.lower() not in GENERIC and k.lower() not in places
    ]
    hits = sorted({k for k in wanted if _has(text_l + " " + title_l, k)}, key=str.lower)
    if wanted and not hits and len(job.text or "") >= FULL_TEXT:
        return None  # title fits, but nothing in the full posting matches what they care about

    pts = 55 * min(1.0, rs) + {"local": 20, "remote": 18, "any": 8}[loc_kind] + min(15, 4 * len(hits))
    pts += min(9, 3 * len(sig.liked_words & set(words(job.title)) - GENERIC))  # like jobs they liked
    pts -= 20 * sum(1 for p in d.get("deal_breakers", []) if len(p.split()) == 1 and _has(text_l, p))
    types, et = set(d.get("employment_types", [])), (job.employment_type or "").lower()
    if types and et:
        if "part-time" in types and "full" in et and "permanent" not in types:
            pts -= 10
        if types <= {"permanent"} and re.search(r"\b(contract|temporary|freelance)\b", et):
            pts -= 10
    if sig.salary_strict and (d.get("salary") or {}).get("minimum") and not (job.salary_min or job.salary_max):
        pts -= 10  # they told us pay was too low before – unknown salary is a risk now
    if job.direct:
        pts += 5

    hits = hits[:3]
    why = (
        [f"matches “{role}”"]
        + ([f"remote ({where})" if loc_kind == "remote" else where] if where else [])
        + (["mentions " + ", ".join(hits)] if hits else [])
    )
    return Match(int(max(0, min(100, pts))), " · ".join(why), role, loc_kind, where, tuple(hits))
