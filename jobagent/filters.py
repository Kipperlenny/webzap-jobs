"""Cheap rule filters per precision profile: title, location, age, stated salary, hard exclusion patterns."""

import re
from datetime import UTC, datetime, timedelta

from .common import REMOTE, to_eur

CONTRACT = re.compile(
    r"\b(interim|fractional|freelance|freelancer|contractor|contract role|b2b contract|temporary|"
    r"consulting mandate|freiberuflich|projektbasis)\b",
    re.I,
)


def title_ok(p, title: str) -> bool:
    if p.title_exclude and p.title_exclude.search(title):
        return False
    return any(rx.search(title) for rx in p.title_include)


def location_class(p, loc: str, text: str, remote_flag: bool) -> str:
    """'local' (home area), 'remote' (remote from an allowed region) or '' (not compatible)."""
    loc = (loc or "").strip()
    if p.home and p.home.search(loc):
        return "local"
    if remote_flag or REMOTE.search(loc):
        if p.remote_ok and p.remote_ok.search(loc):
            return "remote"
        if (not loc or re.fullmatch(r"\W*(remote|anywhere|fully remote)\W*", loc, re.I)) and (
            p.remote_ok
            and p.remote_ok.search(text[:4000])
            and not re.search(r"\b(us|u\.s\.|united states)[- ]only\b", text, re.I)
        ):
            return "remote"
    return ""


def is_contract(title: str, employment_type: str) -> bool:
    return bool(CONTRACT.search(title) or CONTRACT.search(employment_type or ""))


def too_old(p, job) -> bool:
    days = p.search("max_age_days", 45)
    if not job.posted or not job.posted.tzinfo:
        return False
    if job.direct and p.search("direct_ignores_age", True):
        return False  # live on the employer's own ATS right now = still open
    return job.posted < datetime.now(UTC) - timedelta(days=days)


def salary_below_floor(p, job) -> bool:
    floor = p.comp("floor_base", 0)
    top = to_eur(job.salary_max or job.salary_min, job.currency)
    if not floor or top is None or top < 1000:  # no salary, or hourly/daily – leave that to the model
        return False
    return top < floor


# Frequent short words per language – enough to tell which language a job description is written in.
STOPWORDS = {
    "german": frozenset(
        "und der die das nicht mit für wir sie ist auf bei eine einen dich du dein deine ihre ihr "
        "sowie oder auch zu von im den dem ein unser unsere werden wirst bist".split()
    ),
    "english": frozenset("the and of to in for with you we our is are be will on as an or your this that".split()),
    "spanish": frozenset("el la los las de y en con para que un una por del nuestro nuestra tu es se al".split()),
    "french": frozenset("le la les des et en pour avec un une vous nous du est au votre notre sur dans".split()),
    "portuguese": frozenset("o os as em com para que um uma não você nós seu sua do da dos das na no ao".split()),
}


def written_in(text: str) -> str:
    """The language a description is written in ('german', 'english', …), or '' if it is too short to tell."""
    words = re.findall(r"[a-zäöüßàâçéèêëîïôûùñáíóú]+", (text or "").lower())[:600]
    counts = {lang: sum(1 for w in words if w in sw) for lang, sw in STOPWORDS.items()}
    lang, n = max(counts.items(), key=lambda kv: kv[1])
    return lang if n >= 12 and n >= 0.08 * len(words) else ""


def requirement_missing(p, text: str) -> bool:
    """[rules] require_any / require_written_in: the posting must match one pattern or be written in one of the
    languages (e.g. "a language I speak is asked for, or the ad is written in it"). Nothing configured: never missing.
    """
    if not p.require_any and not p.require_written_in:
        return False
    if any(rx.search(text) for rx in p.require_any):
        return False
    return written_in(text) not in p.require_written_in


def hard_excluded(p, text: str) -> str:
    """Return the matching snippet if a hard exclusion pattern hits the description."""
    for rx in p.hard_exclude:
        m = rx.search(text)
        if m:
            return m.group(0)[:80]
    return ""


def prefilter(p, job) -> str:
    """'' if the job passes the rules for this profile, else the rejection reason."""
    if not title_ok(p, job.title):
        return "title"
    job.loc_class = location_class(p, job.location, job.text, job.remote)
    job.contract = is_contract(job.title, job.employment_type)
    if not job.loc_class and not job.contract:
        return "location"
    if too_old(p, job):
        return "too old"
    if salary_below_floor(p, job):
        return "stated salary below floor"
    if hit := hard_excluded(p, job.text):
        return f"excluded: “{hit}”"
    if requirement_missing(p, job.text):
        return "requirement not met"  # not listed as a rejection in the digest: most jobs miss it
    return ""
