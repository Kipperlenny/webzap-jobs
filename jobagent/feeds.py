"""Partner job-search feeds (Adzuna, Jooble, Careerjet). Search APIs: one call per query + country.

Each feed is active only when its key is set in the environment. Their links are tracking redirects through which the
partner may pay us per click, so every job from here carries `partner=<name>` and must be labelled as a partner link.

    ADZUNA_APP_ID / ADZUNA_APP_KEY      https://developer.adzuna.com
    JOOBLE_API_KEY                      https://jooble.org/api/about
    CAREERJET_API_KEY                   https://www.careerjet.com/partners/api
    CAREERJET_USER_IP                   IP reported to Careerjet for server-side searches (your server's public IP)
    CAREERJET_REFERER                   site the results are shown on (default: BASE_URL) – Careerjet requires it
"""

import logging
import os

import requests

from .sources import UA, Job, parse_dt, strip_html

log = logging.getLogger(__name__)
TIMEOUT = 30

# country (English name, lower-case) -> (adzuna country code, careerjet locale). Jooble takes the country as text.
COUNTRIES = {
    "spain": ("es", "es_ES"),
    "germany": ("de", "de_DE"),
    "austria": ("at", "de_AT"),
    "switzerland": ("ch", "de_CH"),
    "united kingdom": ("gb", "en_GB"),
    "france": ("fr", "fr_FR"),
    "netherlands": ("nl", "nl_NL"),
    "italy": ("it", "it_IT"),
    "poland": ("pl", "pl_PL"),
    "portugal": (None, "pt_PT"),
    "belgium": ("be", "fr_BE"),
    "ireland": (None, "en_IE"),
    "united states": ("us", "en_US"),
    "canada": ("ca", "en_CA"),
}


def active() -> list[str]:
    out = []
    if os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY"):
        out.append("adzuna")
    if os.environ.get("JOOBLE_API_KEY"):
        out.append("jooble")
    if os.environ.get("CAREERJET_API_KEY") and os.environ.get("CAREERJET_USER_IP"):
        out.append("careerjet")
    return out


def adzuna(query: str, country: str, where: str = "", n: int = 30) -> list[Job]:
    code = COUNTRIES.get(country.lower(), (None,))[0]
    if not code:
        return []
    params = {
        "app_id": os.environ["ADZUNA_APP_ID"],
        "app_key": os.environ["ADZUNA_APP_KEY"],
        "what": query,
        "results_per_page": n,
        "max_days_old": 30,
        "content-type": "application/json",
    }
    if where:
        params["where"] = where
    r = requests.get(f"https://api.adzuna.com/v1/api/jobs/{code}/search/1", params=params, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("results", []):
        out.append(
            Job(
                "adzuna",
                (j.get("company") or {}).get("display_name", ""),
                strip_html(j.get("title", "")),
                j["redirect_url"],
                location=(j.get("location") or {}).get("display_name", ""),
                remote="remote" in (j.get("title", "") + j.get("description", "")).lower(),
                text=strip_html(j.get("description", "")),
                posted=parse_dt(j.get("created")),
                employment_type=f"{j.get('contract_type') or ''} {j.get('contract_time') or ''}".strip(),
                salary_min=j.get("salary_min"),
                salary_max=j.get("salary_max"),
                currency="GBP" if code == "gb" else "USD" if code == "us" else "EUR",
                partner="Adzuna",
            )
        )
    return out


def jooble(query: str, country: str, where: str = "", n: int = 30) -> list[Job]:
    if country.lower() not in COUNTRIES:
        return []
    # The key only works on jooble.org itself (country subdomains answer 403); the country goes into the location.
    location = ", ".join(filter(None, [where, country.title()]))
    body = {"keywords": query, "location": location, "page": 1, "ResultOnPage": n}
    r = requests.post(f"https://jooble.org/api/{os.environ['JOOBLE_API_KEY']}", json=body, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append(
            Job(
                "jooble",
                j.get("company", ""),
                strip_html(j.get("title", "")),
                j["link"],
                location=j.get("location", ""),
                remote="remote" in (j.get("title", "") + j.get("snippet", "")).lower(),
                text=strip_html(j.get("snippet", "")),
                posted=parse_dt(j.get("updated")),
                employment_type=j.get("type", ""),
                partner="Jooble",
            )
        )
    return out


def careerjet(query: str, country: str, where: str = "", n: int = 30) -> list[Job]:
    locale = COUNTRIES.get(country.lower(), (None, None))[1]
    if not locale:
        return []
    params = {
        "keywords": query,
        "locale_code": locale,
        "page_size": n,
        "sort": "date",
        "user_ip": os.environ["CAREERJET_USER_IP"],
        "user_agent": UA["User-Agent"],
    }
    if where:
        params["location"] = where
    referer = os.environ.get("CAREERJET_REFERER") or os.environ.get("BASE_URL", "")
    r = requests.get(
        "https://search.api.careerjet.net/v4/query",
        params=params,
        auth=(os.environ["CAREERJET_API_KEY"], ""),
        headers=UA | {"Referer": referer.rstrip("/") + "/"},  # Careerjet rejects requests without a declared site
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append(
            Job(
                "careerjet",
                j.get("company", ""),
                strip_html(j.get("title", "")),
                j["url"],
                location=j.get("locations", ""),
                remote="remote" in (j.get("title", "") + j.get("description", "")).lower(),
                text=strip_html(j.get("description", "")),
                posted=parse_dt(j.get("date")),
                salary_min=j.get("salary_min"),
                salary_max=j.get("salary_max"),
                currency=j.get("salary_currency_code"),
                partner="Careerjet",
            )
        )
    return out


FEEDS = {"adzuna": adzuna, "jooble": jooble, "careerjet": careerjet}


def search(query: str, country: str, where: str = "") -> list[Job]:
    """Run one query against every active partner feed; failures are logged, never fatal."""
    out = []
    for name in active():
        try:
            out += [j for j in FEEDS[name](query, country, where) if j.url.startswith(("https://", "http://"))]
        except Exception as e:  # noqa: BLE001
            log.warning("partner feed %s failed for %r/%s: %s", name, query, country, e.__class__.__name__)
    return out
