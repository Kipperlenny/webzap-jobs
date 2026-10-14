"""Job pages: one public page per role and place (pages.toml), listing the open jobs from the daily pool that a
subscriber with that search would get. digest.py saves the matches once a day (`save`); the web app reads them (`get`).

A page's search goes through the digest's own matching, so a page never shows jobs the emails wouldn't send. Job links
go straight to the posting – no redirect, nothing counted.
"""

import json
import os
import time
import tomllib
from collections import Counter
from pathlib import Path

import matching
from settings import DB_PATH

CONFIG = tomllib.loads(Path(os.environ.get("PAGES_CONFIG", "pages.toml")).read_text())
SETTINGS = CONFIG.get("settings", {})
SNAPSHOT = DB_PATH.parent / "job-pages.json"  # shared by the worker (writes) and the web app (reads)
PER_COMPANY = 3  # variety: a page full of one employer's openings says little about the market
PAGES = {(p["role"], p["place"]) for p in CONFIG.get("pages", [])}


def profile(role: str, place: str) -> dict:
    """The page's search in the shape of a subscriber's derived search fields (see extract.py)."""
    r, p = CONFIG["roles"][role], CONFIG["places"][place]
    return {
        "target_roles": r["target_roles"],
        "locations": [{"place": x} for x in p["locations"]],
        "remote_regions": p["remote_regions"],
    }


def matches(index: matching.TitleIndex, role: str, place: str, min_score: int) -> list:
    """Jobs from the pool that fit the page's search, newest first."""
    d, exclude = profile(role, place), set(CONFIG["roles"][role].get("exclude", []))
    found = []
    for job in index.candidates(d["target_roles"]):
        if job.partner:  # paid partner links are labelled in our emails; they don't go on public pages
            continue
        if exclude & set(matching.words(job.title)):
            continue
        m = matching.score(job, d)
        if m and m.score >= min_score:
            found.append(job)
    return sorted(found, key=lambda j: j.posted.timestamp() if j.posted else 0, reverse=True)


def _shown(jobs: list) -> list[dict]:
    out, per_company = [], Counter()
    for j in jobs:
        if len(out) >= SETTINGS.get("max_jobs", 25):
            break
        if per_company[j.company.lower()] >= PER_COMPANY:
            continue
        per_company[j.company.lower()] += 1
        posted = j.posted.date().isoformat() if j.posted else ""
        location = (j.location or "")[:80]
        out.append({"title": j.title, "company": j.company, "location": location, "url": j.url, "posted": posted})
    return out


def save(index: matching.TitleIndex, min_score: int, now: float | None = None):
    pages = {}
    for role, place in sorted(PAGES):
        jobs = matches(index, role, place, min_score)
        pages[f"{role}/{place}"] = {"count": len(jobs), "jobs": _shown(jobs)}
    tmp = SNAPSHOT.with_suffix(".tmp")
    tmp.write_text(json.dumps({"generated": now or time.time(), "pages": pages}, ensure_ascii=False))
    tmp.replace(SNAPSHOT)  # atomic: the web app never reads half a file


_cache: dict = {"mtime": None, "data": None}


def _load() -> dict | None:
    try:
        mtime = SNAPSHOT.stat().st_mtime
    except FileNotFoundError:
        return None
    if mtime != _cache["mtime"]:
        _cache.update(mtime=mtime, data=json.loads(SNAPSHOT.read_text()))
    return _cache["data"]


def _name(names: dict, lang: str) -> str:
    return names.get(lang) or names["en"]


def get(role: str, place: str, lang: str) -> dict | None:
    """What the page shows, or None: unknown page, too few current jobs, or a snapshot too old to trust."""
    if (role, place) not in PAGES or not (data := _load()):
        return None
    if time.time() - data["generated"] > SETTINGS.get("max_age_days", 3) * 86400:
        return None
    entry = data["pages"].get(f"{role}/{place}")
    if not entry or entry["count"] < SETTINGS.get("min_jobs", 5):
        return None
    return {
        "role": _name(CONFIG["roles"][role]["name"], lang),
        "place": _name(CONFIG["places"][place]["name"], lang),
        "count": entry["count"],
        "jobs": entry["jobs"],
        "date": time.strftime("%Y-%m-%d", time.gmtime(data["generated"])),
    }


def paths() -> list[str]:
    """The pages that currently exist, for the sitemap."""
    return [f"/jobs/{role}/{place}" for role, place in sorted(PAGES) if get(role, place, "en")]
