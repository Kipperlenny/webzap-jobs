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
    return ""
