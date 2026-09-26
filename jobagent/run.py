"""Daily run: fetch once → per precision profile: rules → verify → model assessment → capped digest email."""

import argparse
import concurrent.futures as cf
import logging
import time
from datetime import UTC, datetime, timedelta

from . import assess, filters, mailer, report
from .common import load_env, url_alive
from .config import ENV_FILES, EXAMPLE_PROFILE, PRIVATE_PROFILES, REPORT_DIR
from .dedup import dedupe
from .profile import discover
from .sources import enrich, fetch, merge_specs
from .store import Store

log = logging.getLogger("jobagent")


def wait_for_llm(max_hours: float):
    """The model server is not always online: poll with growing pauses up to max_hours."""
    deadline, n = time.time() + max_hours * 3600, 0
    while True:
        picked = assess.pick_connector()
        if picked or time.time() >= deadline:
            return picked
        n += 1
        wait = min(1800, 60 * 2 ** (n - 1), max(0, deadline - time.time()))
        log.info("no LLM connector online – retrying in %ds", wait)
        time.sleep(wait)


def due_externals(p, store) -> list[dict]:
    """The profile's [[external]] links not shown in the last `external_repeat_days` – a reminder, not a newsletter."""
    days = p.out("external_repeat_days", 30)
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    shown = store.externals_shown(p.id)
    return [x for x in p.externals if shown.get(x["url"], "") < cutoff][: p.out("max_externals", 3)]


def run_profile(p, jobs, stats, store, args):
    started = datetime.now(UTC).isoformat(timespec="seconds")
    candidates, rejected_rules = [], []
    for j in jobs:  # already deduplicated across sources (main)
        if not filters.title_ok(p, j.title):
            continue
        reason = filters.prefilter(p, enrich(j) if j.source == "smartrecruiters" else j)
        if reason.startswith("excluded"):
            rejected_rules.append((j, reason))
        elif not reason:
            candidates.append(j)
    with cf.ThreadPoolExecutor(8) as ex:
        alive = list(ex.map(url_alive, candidates))
    candidates = [j for j, ok in zip(candidates, alive, strict=False) if ok]
    log.info("[%s] %d candidates after rules and link check", p.name, len(candidates))

    todo, judged, fresh = [], [], []
    for j in candidates:
        prev = store.get(p.id, j.all_keys)
        if prev and prev["emailed_at"] and not args.resend:
            continue
        if prev and assess.reusable(p, prev["assessment"]) and not args.reassess:
            j.assessment = prev["assessment"]
            judged.append(j)
        else:
            todo.append(j)
    # Most promising first: employer ATS, home area, recent.
    todo.sort(key=lambda j: (not j.direct, j.loc_class != "local", -(j.posted.timestamp() if j.posted else 0)))
    todo = todo[: args.max_llm if args.max_llm is not None else p.out("max_llm_jobs", 25)]

    note = ""
    if todo:
        picked = wait_for_llm(args.wait_hours)
        if not picked:
            note = "no LLM online – assessment postponed"
            log.warning("[%s] %s; %d jobs stay queued for the next run", p.name, note, len(todo))
            todo = []
        else:
            conn, model = picked
            for i, j in enumerate(todo, 1):
                t = time.time()
                try:
                    j.assessment = assess.assess(conn, model, p, j)
                    judged.append(j)
                    fresh.append(j)
                    log.info(
                        "[%s] %d/%d %s – %s: %s (%.0fs)",
                        p.name,
                        i,
                        len(todo),
                        j.company,
                        j.title,
                        j.assessment["total"],
                        time.time() - t,
                    )
                except Exception as e:  # noqa: BLE001 – offline mid-run, bad JSON, … → retried next run
                    log.warning("[%s] assessment failed for %s: %s", p.name, j.url, e)
                    if isinstance(e, assess.connectors.Unavailable):
                        note = "LLM went offline during the run"
                        break

    for j in judged:
        j.status, reason = assess.classify(p, j, j.assessment)
        j.score = j.assessment["total"]
        store.upsert(p.id, j, j.status, reason, j.assessment)
    new_rule_rejects = [(j, r) for j, r in rejected_rules if not store.get(p.id, j.all_keys)]
    for j, reason in new_rule_rejects:
        store.upsert(p.id, j, "rejected", reason)
    store.commit()

    caps = {
        "strong": p.out("max_strong", 3),
        "possible": p.out("max_possible", 2),
        "contract": p.out("max_contract", 2),
    }
    picked = {k: sorted([j for j in judged if j.status == k], key=lambda j: -j.score)[:n] for k, n in caps.items()}
    # Rejected list (shown once, so false positives aren't rediscovered): newly judged near-misses + rule exclusions.
    notable = sorted(
        [j for j in fresh if j.status == "rejected" and j.score >= p.out("possible_min", 60) - 15],
        key=lambda j: -j.score,
    )[:5]
    notable_rules = new_rule_rejects[:5]

    externals = due_externals(p, store)
    body_html, body_text = report.render(
        p, picked, notable, notable_rules, stats, len(jobs), len(candidates), note, externals
    )
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"{datetime.now():%Y-%m-%d}-{p.path.stem}.html"
    report_path.write_text(body_html)
    n = sum(len(v) for v in picked.values())
    if args.dry_run:
        print(body_text)
    else:
        subject = (
            (
                f"Job matches: {len(picked['strong'])} strong, {len(picked['possible'])} possible, "
                f"{len(picked['contract'])} contract – {datetime.now():%d.%m.%Y}"
            )
            if n
            else f"Job matches: none today – {datetime.now():%d.%m.%Y}"
        )
        mailer.send(subject, body_html, body_text, to=args.to or p.email)
        store.mark_emailed(p.id, [j.key for v in picked.values() for j in v])
        store.mark_externals_shown(p.id, [x["url"] for x in externals])
    store.log_run(p.id, started, scanned=len(jobs), candidates=len(candidates), assessed=len(judged), sent=n, note=note)
    store.commit()
    log.info("[%s] report %s, %d jobs in digest", p.name, report_path, n)


def main():
    ap = argparse.ArgumentParser(description="WebZap Jobs – precision job search per profile")
    ap.add_argument("profiles", nargs="*", help="profile TOML files (default: all in private/profiles/)")
    ap.add_argument("--dry-run", action="store_true", help="print the digest, send nothing")
    ap.add_argument("--to", help="send to this address instead of the profile's email")
    ap.add_argument("--resend", action="store_true", help="include jobs that were already emailed")
    ap.add_argument("--reassess", action="store_true", help="re-run the model on already assessed jobs")
    ap.add_argument("--max-llm", type=int, help="override the profile's max_llm_jobs (for testing)")
    ap.add_argument("--wait-hours", type=float, default=6, help="how long to wait for an offline LLM (default 6)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env(ENV_FILES)
    profiles = discover(args.profiles, PRIVATE_PROFILES, EXAMPLE_PROFILE)
    jobs, stats = fetch(
        merge_specs([p.sources for p in profiles]),
        queries=sorted({q for p in profiles for q in p.search("queries", [])}),
        jobicy_geo=sorted({g for p in profiles for g in p.sources.get("jobicy_geo", [])}),
    )
    store = Store()
    copies = len(jobs)
    jobs = dedupe(jobs, store.aliases())  # once for all profiles: fewer model calls, no job twice
    log.info(
        "fetched %d postings (%d distinct) from %d sources for %d profile(s)",
        copies,
        len(jobs),
        len(stats),
        len(profiles),
    )
    for p in profiles:
        run_profile(p, jobs, stats, store, args)


if __name__ == "__main__":
    main()
