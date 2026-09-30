"""👍/👎 on the job agent's digest, and what the pipeline learns from them.

The digest links to the web app's feedback page with a signed token that carries the job itself (profile, key, title,
company), so the web app needs no copy of the agent's database: it checks the signature, shows the usual POST page and
stores the vote. Each run pulls the new votes back (`sync`). Votes can also be cast on the command line
(`python3 -m jobagent.feedback`), e.g. from a chat with a coding assistant.

What a vote teaches (`apply`): 👍 = more like this title and this company (the company is followed, see companies.py);
👎 "not this company" = never this company again; 👎 "not my field" / "too junior" / "too senior" = never this title
again. Every recent vote is also shown to the model as an example when it judges titles, companies and postings.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import UTC, datetime

import requests

from .dedup import company_key
from .titles import norm

log = logging.getLogger("jobagent")

# Stored in English; the web form shows them translated (feedback.reasons.<key>).
REASONS = {
    "field": "not my field",
    "location": "wrong location",
    "junior": "too junior",
    "senior": "too senior",
    "salary": "salary too low",
    "company": "not this company",
    "known": "already applied / known",
    "other": "other",
}
TITLE_REASONS = {REASONS[k] for k in ("field", "junior", "senior")}
PREFIX = "a."  # tells agent tokens apart from the web digest's random ones
MAX_EXAMPLES = 30


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _secret() -> bytes:
    """Derived from HASH_PEPPER, which the web app and the agent already share through .env."""
    pepper = os.environ.get("HASH_PEPPER", "")
    return hmac.new(pepper.encode(), b"jobagent-feedback", hashlib.sha256).digest() if pepper else b""


def _base_url() -> str:
    return os.environ.get("BASE_URL", "").rstrip("/")


def enabled() -> bool:
    return bool(_secret() and _base_url())


def _sign(payload: str) -> str:
    return _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()[:16])


def token(profile: str, key: str, title: str, company: str) -> str:
    data = {"p": profile, "k": key, "t": title[:100], "c": company[:60]}
    payload = _b64(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode())
    return f"{PREFIX}{payload}.{_sign(payload)}"


def verify(tok: str) -> dict | None:
    """The job a token stands for ({"p", "k", "t", "c"}), or None if it is not a valid agent token."""
    if not tok.startswith(PREFIX) or not _secret():
        return None
    payload, _, sig = tok[len(PREFIX) :].rpartition(".")
    if not payload or not hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        data = json.loads(_unb64(payload))
    except ValueError:
        return None
    return data if isinstance(data, dict) and {"p", "k", "t", "c"} <= data.keys() else None


def links(profile: str, job) -> dict:
    """{"up": url, "down": url} for a digest entry, or {} when the instance has no BASE_URL / HASH_PEPPER."""
    if not enabled():
        return {}
    url = f"{_base_url()}/feedback/{token(profile, job.key, job.title, job.company)}"
    return {"up": f"{url}?v=up", "down": f"{url}?v=down"}


def short_id(profile: str, key: str) -> str:
    """The id a job is shown with in the digest, for votes on the command line ("vote 3f9a2c1 down")."""
    return hashlib.sha256(f"{profile}|{key}".encode()).hexdigest()[:7]


def api_key() -> str:
    """Bearer key for the web app's vote export."""
    return hmac.new(_secret(), b"export", hashlib.sha256).hexdigest()


def sync(store) -> int:
    """Pull new email votes from the web app into the agent's database. Returns how many arrived."""
    if not enabled():
        return 0
    last = store.last_vote_at()
    since = int(datetime.fromisoformat(last).timestamp()) if last else 0
    try:
        r = requests.get(
            f"{_base_url()}/agent-feedback",
            params={"since": since},
            headers={"Authorization": f"Bearer {api_key()}"},
            timeout=30,
        )
        r.raise_for_status()
        votes = r.json().get("votes", [])
    except (requests.RequestException, ValueError) as e:
        log.warning("could not fetch email votes: %s", e.__class__.__name__)
        return 0
    for v in votes:
        at = datetime.fromtimestamp(int(v["at"]), UTC).isoformat(timespec="seconds")
        store.set_vote(v["profile"], v["key"], v["title"], v["company"], v["vote"], v["reason"], "email", at)
    store.commit()
    if votes:
        log.info("%d new vote(s) from email", len(votes))
    return len(votes)


def apply(p, store):
    """Turn the person's votes and company verdicts into the profile's runtime rules and model examples."""
    votes = store.votes(p.id)
    verdicts = store.company_verdicts(p.id)
    down = [v for v in votes if v["vote"] == "down"]
    up = [v for v in votes if v["vote"] == "up"]
    p.blocked_titles = {norm(v["title"]) for v in down if TITLE_REASONS & set(v["reason"].split("; "))}
    p.liked_titles = {norm(v["title"]) for v in up}
    p.blocked_companies = (
        {company_key(n) for n in p.block_companies}
        | {k for k, v in verdicts.items() if not v["fits"] and v["origin"] == "you"}
        | {company_key(v["company"]) for v in down if REASONS["company"] in v["reason"]}
    )
    liked = {company_key(n): n for n in p.like_companies}
    liked |= {k: v["name"] for k, v in verdicts.items() if v["fits"]}
    liked |= {company_key(v["company"]): v["company"] for v in up if v["company"]}
    p.liked_companies = {k: n for k, n in liked.items() if k and k not in p.blocked_companies}
    p.examples = [
        f"{'LIKED' if v['vote'] == 'up' else 'DISLIKED'}: {v['title']} – {v['company']}"
        + (f" ({v['reason']})" if v["reason"] else "")
        for v in votes[:MAX_EXAMPLES]
    ]
