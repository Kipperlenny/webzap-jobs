"""Subscriber digest: match open jobs to every confirmed subscriber's derived search fields and email them.

Run daily (e.g. cron: docker exec webzap-jobs-worker python digest.py). Daily subscribers get a digest every day with
new matches, weekly subscribers on `weekly_day`; the very first digest goes out on the next run after confirmation.

    python digest.py --dry-run            # print what would be sent, change nothing
    python digest.py --only you@x.com     # one subscriber (with --force: even if not due)
"""

import argparse
import concurrent.futures as cf
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
from jobagent.dedup import dedupe
from jobagent.sources import Job, fetch

import external
import extract
import mailer
import matching
import store

log = logging.getLogger("webzap-jobs.digest")
CONFIG = tomllib.loads(Path(os.environ.get("DIGEST_CONFIG", "digest.toml")).read_text())
SPONSORED_DIR = Path(os.environ.get("SPONSORED_DIR", "/sponsored"))
PARTNER_WORKERS = 6  # parallel partner searches – polite to the partner APIs, still fast with many subscribers


# ---------- job pool ----------


def partner_searches(d: dict) -> set[tuple[str, str, str]]:
    """The partner-feed searches one subscriber needs: (query, country, place), normalised so that subscribers with
    the same search share one request."""
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
    return {
        (" ".join(q.lower().split()), country, " ".join(place.lower().split()))
        for q in queries
        for country, place in list(targets.items())[:2]
        if q.strip()
    }


def fetch_pool(recipients: list[dict], dry_run: bool = False) -> list[Job]:
    """Everything that can be sent today, fetched once for everyone and deduplicated: the boards and aggregators in
    digest.toml plus the partner-feed searches of all due subscribers (each distinct search runs once)."""
    src = CONFIG["sources"]
    jobs, _ = fetch(src, queries=src.get("himalayas_queries", []), jobicy_geo=src.get("jobicy_geo", []))
    if feeds.active():
        searches = sorted(set().union(*(partner_searches(r["derived"]) for r in recipients)))
        with cf.ThreadPoolExecutor(PARTNER_WORKERS) as ex:
            for found in ex.map(lambda s: feeds.search(*s), searches):
                jobs += found
        log.info("partner feeds: %d distinct searches for %d subscribers", len(searches), len(recipients))
    cutoff = time.time() - CONFIG["digest"]["max_age_days"] * 86400
    fresh = [j for j in jobs if not (j.posted and j.posted.timestamp() < cutoff and not j.direct)]
    pool = dedupe(fresh, store.alias_book(read_only=dry_run))
    log.info("job pool: %d postings, %d after merging copies of the same job", len(fresh), len(pool))
    return pool


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
        if job.key in already:  # sponsored jobs are ours: one copy, its own key
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


def select(r: dict, index: matching.TitleIndex, sponsored: list[dict]) -> list[Pick]:
    """The few best new jobs for one subscriber (strict score threshold, per-company cap, live links only)."""
    cfg, d = CONFIG["digest"], r["derived"]
    already = store.sent_keys(r["id"])
    sig = matching.Signals(**store.feedback_signals(r["id"]))  # learned from their 👍/👎
    scored = []
    for job in index.candidates(d.get("target_roles") or []):
        if any(k in already for k in job.all_keys):  # sent before – maybe as another copy of the same job
            continue
        m = matching.score(job, d, sig)
        if m and m.score >= cfg["min_score"]:
            scored.append(Pick(job, m))
    scored.sort(key=lambda p: (-p.match.score, not p.job.direct))
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


def externals_for(r: dict) -> list[dict]:
    """External links (sites we can't search) that fit this person and weren't shown to them recently."""
    s = external.SETTINGS
    fitting = external.suggest(r["derived"], r["lang"])
    due = set(store.externals_due(r["id"], [x["url"] for x in fitting], s.get("repeat_days", 30)))
    return [x for x in fitting if x["url"] in due][: s.get("max_per_email", 2)]


def send(r: dict, picks: list[Pick], externals: list[dict]):
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
        externals=externals,
    )
    store.mark_externals_shown(r["id"], [x["url"] for x in externals])
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
    index = matching.TitleIndex(fetch_pool(recipients, args.dry_run))
    sponsored, sent = load_sponsored(), 0
    for r in recipients:
        picks = select(r, index, sponsored)
        first_needs_help = not r["first_digest_at"] and extract.needs_help(r["derived"])
        if not picks and not first_needs_help:
            log.info("subscriber %s: nothing good enough – no email", r["id"])
            continue
        externals = externals_for(r)  # only ever added to an email we send anyway
        if args.dry_run:
            print(f"\n=== {r['email']} ({r['frequency']}): {len(picks)} job(s)")
            for p in picks:
                tag = "[sponsored] " if p.sponsor_id else f"[{p.job.partner}] " if p.job.partner else ""
                print(f"  [{p.match.score}] {tag}{p.job.title} — {p.job.company} ({p.job.location[:50]})")
                print(f"       {p.match.why}")
            for x in externals:
                print(f"  [external] {x['name']}: {x['url']}")
            continue
        send(r, picks, externals)
        sent += 1
        log.info("subscriber %s: sent %d job(s), %d external link(s)", r["id"], len(picks), len(externals))
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
