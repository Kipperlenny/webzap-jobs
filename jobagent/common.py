"""Small helpers shared by the job agent and the web app."""

import ipaddress
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

import requests

REMOTE = re.compile(
    r"\b(remote|remoto|anywhere|distributed|home ?office|home[- ]based|work from home|fully remote)\b", re.I
)
# Approximate FX to EUR – good enough for salary floor checks.
FX = {
    "EUR": 1.0,
    "USD": 0.86,
    "GBP": 1.16,
    "CHF": 1.06,
    "PLN": 0.23,
    "SEK": 0.088,
    "DKK": 0.134,
    "NOK": 0.085,
    "CAD": 0.62,
}
SOURCE_NAMES = {
    "remotive": "Remotive",
    "remoteok": "Remote OK",
    "arbeitnow": "Arbeitnow",
    "himalayas": "Himalayas",
    "jobicy": "Jobicy",
}
DEAD_PAGE = (
    "job is no longer available",
    "this job has expired",
    "position has been filled",
    "no longer accepting applications",
    "job not found",
    "this position is closed",
)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) webzap-jobs/1.0 (+https://github.com/Kipperlenny/webzap-jobs)"


def to_eur(amount, currency: str | None) -> float | None:
    """Convert an annual amount to EUR; None if it isn't a usable number."""
    try:
        return float(amount) * FX.get((currency or "EUR").upper(), 1.0)
    except (TypeError, ValueError):
        return None


def source_parts(job) -> tuple[str, str]:
    """(kind, name) of a job's source: ('partner', 'Jooble'), ('direct', ''), ('source', 'Greenhouse')."""
    if job.partner:
        return "partner", job.partner
    if job.direct:
        return "direct", ""
    return "source", SOURCE_NAMES.get(job.source, job.source)


def source_label(job) -> str:
    """Attribution shown next to every job (the aggregators' API terms require naming them)."""
    kind, name = source_parts(job)
    return {"partner": f"via {name}", "direct": "employer's career page", "source": f"source: {name}"}[kind]


def is_public_http_url(url: str) -> bool:
    """Only http(s) URLs whose host resolves to public addresses – job links come from third parties, so this keeps
    link checks from being pointed at internal services (SSRF) and keeps javascript:/data: links out of emails."""
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        for info in socket.getaddrinfo(u.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                return False
        return True
    except (ValueError, OSError):
        return False


_alive: dict[str, bool] = {}


def url_alive(job) -> bool:
    """Is the posting still open? Employer ATS and partner search results are live by definition."""
    if job.direct or job.partner:
        return True
    if job.url not in _alive:
        ok = False
        if is_public_http_url(job.url):
            try:
                r = requests.get(job.url, headers={"User-Agent": USER_AGENT}, timeout=20)
                ok = r.status_code < 400 and not any(p in r.text.lower() for p in DEAD_PAGE)
            except requests.RequestException:
                ok = False
        _alive[job.url] = ok
    return _alive[job.url]


def load_env(files: list[Path]):
    """Minimal .env loader (KEY=value lines); existing environment variables win."""
    for f in files:
        if f.exists():
            for line in f.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
