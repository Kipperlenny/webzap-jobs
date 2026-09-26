"""External links: job sites we can't search automatically, offered with a search already filled in for the person.

The catalog is external.toml (see its header for the rules). `suggest()` returns the sites that fit one person, most
specific first; the digest adds the few that weren't shown recently – and only to an email it sends anyway.
"""

import os
import re
import tomllib
from pathlib import Path
from urllib.parse import quote, quote_plus

CATALOG = tomllib.loads(Path(os.environ.get("EXTERNAL_CONFIG", "external.toml")).read_text())
SETTINGS = CATALOG.get("settings", {})
REGIONS = {"dach": ["germany", "austria", "switzerland"], "uk": ["united kingdom"], "great britain": ["united kingdom"]}
# Language names as people write them → the names used in the catalog; email language codes too.
LANGUAGES = {
    "german": "german",
    "deutsch": "german",
    "de": "german",
    "english": "english",
    "englisch": "english",
    "en": "english",
    "spanish": "spanish",
    "español": "spanish",
    "castellano": "spanish",
    "spanisch": "spanish",
    "es": "spanish",
    "french": "french",
    "français": "french",
    "französisch": "french",
    "fr": "french",
}


def _slug(s: str) -> str:
    return quote(re.sub(r"[^\w]+", "-", s.lower()).strip("-"))


def _countries(d: dict) -> set[str]:
    names = [p.get("country", "") for p in d.get("locations") or []] + list(d.get("remote_regions") or [])
    out = set()
    for n in (x.strip().lower() for x in names if x):
        out.update(REGIONS.get(n, [n]))
    return out


def _languages(d: dict, lang: str) -> set[str]:
    words = {w for x in d.get("languages") or [] for w in re.findall(r"[^\W\d_]+", x.lower())}
    return {LANGUAGES[w] for w in words | {lang} if w in LANGUAGES}


def _fits(site: dict, d: dict, lang: str, blob: str) -> bool:
    if site.get("countries") and not _countries(d) & set(site["countries"]):
        return False
    if site.get("languages") and not _languages(d, lang) & set(site["languages"]):
        return False
    if site.get("types") and not set(d.get("employment_types") or []) & set(site["types"]):
        return False
    topics = site.get("topics") or []
    return not topics or any(re.search(r"\b" + re.escape(t) + r"\b", blob) for t in topics)


def _place(d: dict) -> str:
    """The person's first place – unless they only want remote work, where a place would narrow things wrongly."""
    if set(d.get("work_modes") or []) == {"remote"}:
        return ""
    return next((p.get("place", "") for p in d.get("locations") or [] if p.get("place")), "")


def suggest(d: dict, lang: str) -> list[dict]:
    """Every catalog site that fits this person, most specific (field-specific) first, each with a prepared search:
    {"name", "url", "query", "where"}. Empty if we don't know what they search for."""
    # The plain role title: generated search queries often carry places ("… remote Europe") that sites misread.
    query = next(iter((d.get("target_roles") or []) + (d.get("search_queries") or [])), "").strip()[:60]
    if not query:
        return []
    blob = " ".join(
        str(x)
        for k in ("target_roles", "fields", "skills", "keywords", "industries_preferred", "company_preferences")
        for x in d.get(k) or []
    ).lower()
    where = _place(d)
    out = []
    for site in sorted(CATALOG.get("site", []), key=lambda s: not s.get("topics")):
        if not _fits(site, d, lang, blob):
            continue
        with_place = bool(where and site.get("search_where"))
        url = (site["search_where"] if with_place else site["search"]).format(
            q=quote_plus(query), q_slug=_slug(query), where=quote_plus(where), where_slug=_slug(where)
        )
        out.append({"name": site["name"], "url": url, "query": query, "where": where if with_place else ""})
    return out
