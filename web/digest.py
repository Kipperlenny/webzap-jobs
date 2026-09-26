"""Subscriber digest: match open jobs to every confirmed subscriber's derived search fields and email them.

Run daily (e.g. cron: docker exec webzap-jobs-worker python digest.py). Daily subscribers get a digest every day with
new matches, weekly subscribers on `weekly_day`; the very first digest goes out on the next run after confirmation.

    python digest.py --dry-run            # print what would be sent, change nothing
    python digest.py --only you@x.com     # one subscriber (with --force: even if not due)
"""

import argparse
import logging
import os
import time
import tomllib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jobagent import feeds
from jobagent.common import url_alive
from jobagent.sources import Job, fetch

import extract
import mailer
import matching
import store

log = logging.getLogger("webzap-jobs.digest")
CONFIG = tomllib.loads(Path(os.environ.get("DIGEST_CONFIG", "digest.toml")).read_text())
SPONSORED_DIR = Path(os.environ.get("SPONSORED_DIR", "/sponsored"))


# ---------- job pool ----------


def fetch_pool() -> list[Job]:
    src = CONFIG["sources"]
    jobs, _ = fetch(src, queries=src.get("himalayas_queries", []), jobicy_geo=src.get("jobicy_geo", []))
    cutoff = time.time() - CONFIG["digest"]["max_age_days"] * 86400
    return [j for j in jobs if not (j.posted and j.posted.timestamp() < cutoff and not j.direct)]


def partner_jobs(d: dict, cache: dict) -> list[Job]:
    """Searches against the partner feeds for one subscriber, cached per (query, country, place)."""
    if not feeds.active():
        return []
    n = CONFIG["digest"]["partner_queries_per_subscriber"]
    queries = (d.get("search_queries") or d.get("target_roles") or [])[:n]
    # One search per distinct supported country; country-wide for people open to remote work, else their first place.
    remote_ok = "remote" in d.get("work_modes", [])
    targets = {}
    for loc in d.get("locations") or []:
        country = (loc.get("country") or "").lower()
        if country in feeds.COUNTRIES and country not in targets:
            targets[country] = "" if remote_ok else loc.get("place", "")
    for region in d.get("remote_regions", []):  # remote-only profiles: regions that are countries
        if region.lower() in feeds.COUNTRIES:
            targets.setdefault(region.lower(), "")
    targets = list(targets.items())[:2]
    out = []
    for q in queries:
        for country, place in targets:
            key = (q.lower(), country.lower(), place.lower())
            if key not in cache:
                cache[key] = feeds.search(q, country, place)
            out += cache[key]
    return out


# ---------- sponsored jobs (one-way: sponsors never get subscriber data) ----------


def load_sponsored() -> list[dict]:
    """private/sponsored/*.toml: one [[job]] per paid listing with its own targeting (see README)."""
    today = datetime.now().date().isoformat()
    out = []
    for f in sorted(SPONSORED_DIR.glob("*.toml")) if SPONSORED_DIR.exists() else []:
        for s in tomllib.loads(f.read_text()).get("job", []):
            if str(s.get("starts", "")) <= today <= str(s.get("ends", "9999")):
                out.append(s)
    return out


def sponsored_match(sponsored: list[dict], d: dict, already: set, sig: matching.Signals):
    """Best-matching active sponsored job this person hasn't seen: (campaign, job, match) or None."""
    best = None
    for s in sponsored:
        job = Job(
            "sponsored",
            s["company"],
            s["title"],
            s["url"],
            location=s.get("location", ""),
            remote=bool(s.get("remote")),
            text=s.get("description", ""),
            direct=True,
        )
        if job.key in already:
            continue
        m = matching.score(job, d, sig)
        if m and m.score >= CONFIG["digest"]["min_score"] and (not best or m.score > best[2].score):
            best = (s, job, m)
    return best


# ---------- sending ----------


def due(r: dict, force: bool) -> bool:
    if force or not r["first_digest_at"]:
        return True
    since = time.time() - (r["last_digest_at"] or 0)
    if r["frequency"] == "daily":
        return since > 20 * 3600
    return datetime.now().weekday() == CONFIG["digest"]["weekly_day"] and since > 6 * 86400


@dataclass
class Pick:
    job: Job
    match: matching.Match
    sponsor_id: str = ""  # set for paid placements


def select(r: dict, pool: list[Job], sponsored: list[dict], cache: dict) -> list[Pick]:
    """The few best new jobs for one subscriber (strict score threshold, per-company cap, live links only)."""
    cfg, d = CONFIG["digest"], r["derived"]
    already = store.sent_keys(r["id"])
    sig = matching.Signals(**store.feedback_signals(r["id"]))  # learned from their 👍/👎
    scored, seen = [], set()
    for job in pool + partner_jobs(d, cache):
        if job.key in already or job.key in seen:
            continue
        m = matching.score(job, d, sig)
        if m and m.score >= cfg["min_score"]:
            seen.add(job.key)
            scored.append(Pick(job, m))
    scored.sort(key=lambda p: (-p.match.score, not p.job.direct))  # employer postings beat copies of the same job
    cap = cfg["daily_max"] if r["frequency"] == "daily" else cfg["weekly_max"]
    picks, per_company = [], Counter()
    for p in scored:
        if len(picks) >= cap:
            break
        company = p.job.company.lower()
        if per_company[company] < cfg["max_per_company"] and url_alive(p.job):
            picks.append(p)
            per_company[company] += 1
    if (sp := sponsored_match(sponsored, d, already, sig)) is not None:
        campaign, job, m = sp
        picks.insert(min(2, len(picks)), Pick(job, m, sponsor_id=campaign["id"]))
    return picks


def send(r: dict, picks: list[Pick]):
    token, lang = store.manage_token_for(r["id"]), r["lang"]
    items = [
        mailer.digest_item(
            p.job,
            p.match,
            lang,
            mailer.link(lang, "/feedback/" + store.record_sent(r["id"], p.job, p.match, p.sponsor_id)),
            sponsored=bool(p.sponsor_id),
        )
        for p in picks
    ]
    mailer.send_digest(
        r["email"],
        lang,
        token,
        items,
        # First email only: the model's personal opening line (written during the text analysis, so it's available
        # even when the model server is offline now) and, for vague texts, the "help us understand you" box.
        intro=None if r["first_digest_at"] else r["derived"].get("intro"),
        help=None if r["first_digest_at"] else mailer.help_box(r["derived"], token, lang),
        frequency=r["frequency"],
        disclosure=any(it["sponsored"] or it["partner"] for it in items),
    )
    store.mark_digest_sent(r["id"])


def run(args):
    skip = {x.strip().lower() for x in os.environ.get("DIGEST_SKIP_EMAILS", "").split(",") if x.strip()}
    recipients = [
        r
        for r in store.recipients()
        if r["derived"]
        and r["email"].lower() not in skip
        and (not args.only or r["email"].lower() == args.only.lower())
        and due(r, args.force)
    ]
    log.info("%d subscriber(s) due", len(recipients))
    if not recipients:
        return
    pool = fetch_pool()
    log.info("job pool: %d postings; partner feeds: %s", len(pool), feeds.active() or "none")
    sponsored, cache, sent = load_sponsored(), {}, 0
    for r in recipients:
        picks = select(r, pool, sponsored, cache)
        first_needs_help = not r["first_digest_at"] and extract.needs_help(r["derived"])
        if not picks and not first_needs_help:
            log.info("subscriber %s: nothing good enough – no email", r["id"])
            continue
        if args.dry_run:
            print(f"\n=== {r['email']} ({r['frequency']}): {len(picks)} job(s)")
            for p in picks:
                tag = "[sponsored] " if p.sponsor_id else f"[{p.job.partner}] " if p.job.partner else ""
                print(f"  [{p.match.score}] {tag}{p.job.title} — {p.job.company} ({p.job.location[:50]})")
                print(f"       {p.match.why}")
            continue
        send(r, picks)
        sent += 1
        log.info("subscriber %s: sent %d job(s)", r["id"], len(picks))
    log.info("digests sent: %d", sent)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="only this subscriber email")
    ap.add_argument("--force", action="store_true", help="send even if not due")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
