"""Daily run: fetch once → per precision profile: rules → verify → model assessment → capped digest email."""

import argparse
import concurrent.futures as cf
import logging
import time
from datetime import UTC, datetime, timedelta

from . import assess, companies, connectors, filters, mailer, report, titles, votes
from .common import load_env, url_alive
from .config import ENV_FILES, EXAMPLE_PROFILE, PRIVATE_PROFILES, REPORT_DIR
from .dedup import company_key, dedupe
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


class LLM:
    """The model connection, waited for at most once per run – an offline model must not hold up every profile."""

    def __init__(self, wait_hours: float):
        self.wait_hours, self.tried, self.picked = wait_hours, False, None

    def get(self):
        if not self.tried:
            self.picked, self.tried = wait_for_llm(self.wait_hours), True
        return self.picked


def learn_titles(p, unknown: dict[str, list], learned: dict, store, llm) -> tuple[list[str], str]:
    """Ask the model about titles the patterns don't cover (one batched call, capped per run). Returns the accepted
    titles and a note if the model was offline; undecided titles are asked again next run."""
    if not unknown:
        return [], ""
    if not (picked := llm.get()):
        return [], "no LLM online – new titles not checked"
    asked = dict(list(unknown.items())[: p.raw["titles"].get("learn_max", 300)])
    note = ""
    try:
        verdicts = titles.judge(*picked, p, {t: jobs[0].title for t, jobs in asked.items()})
    except connectors.Unavailable as e:
        verdicts, note = {}, "LLM went offline during the title check"
        log.warning("[%s] %s: %s", p.name, note, e)
    store.save_title_verdicts(p.id, titles.fingerprint(p), verdicts)
    store.commit()
    learned.update(verdicts)
    accepted = [asked[t][0].title for t, ok in verdicts.items() if ok]
    log.info("[%s] %d new titles checked, %d accepted: %s", p.name, len(verdicts), len(accepted), "; ".join(accepted))
    return accepted, note


def due_externals(p, store) -> list[dict]:
    """The profile's [[external]] links not shown in the last `external_repeat_days` – a reminder, not a newsletter."""
    days = p.out("external_repeat_days", 30)
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    shown = store.externals_shown(p.id)
    return [x for x in p.externals if shown.get(x["url"], "") < cutoff][: p.out("max_externals", 3)]


def run_profile(p, jobs, stats, store, args, llm, learned_today=None):
    started = datetime.now(UTC).isoformat(timespec="seconds")
    candidates, rejected_rules, unknown = [], [], {}
    learned = store.title_verdicts(p.id, titles.fingerprint(p))

    def check(j):
        reason = filters.prefilter(p, enrich(j), learned)  # enrich: descriptions of list-only sources
        if reason.startswith("excluded"):
            rejected_rules.append((j, reason))
        elif not reason:
            candidates.append(j)

    for j in jobs:  # already deduplicated across sources (main)
        if filters.title_ok(p, j.title, learned):
            check(j)
        elif filters.worth_asking(p, j, learned):
            unknown.setdefault(titles.norm(j.title), []).append(j)
    new_titles, note = learn_titles(p, unknown, learned, store, llm)
    for t in new_titles:
        for j in unknown[titles.norm(t)]:
            check(j)
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
    # Most promising first: wanted companies, employer ATS, home area, recent.
    todo.sort(
        key=lambda j: (
            company_key(j.company) not in p.liked_companies,
            not j.direct,
            j.loc_class != "local",
            -(j.posted.timestamp() if j.posted else 0),
        )
    )
    todo = todo[: args.max_llm if args.max_llm is not None else p.out("max_llm_jobs", 25)]

    if todo:
        picked = llm.get()
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
        p,
        picked,
        notable,
        notable_rules,
        stats,
        len(jobs),
        len(candidates),
        note,
        externals,
        {**(learned_today or {}), "titles": new_titles},
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
    ap.add_argument("--no-discovery", action="store_true", help="skip company discovery this run")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env(ENV_FILES)
    profiles = discover(args.profiles, PRIVATE_PROFILES, EXAMPLE_PROFILE)
    store = Store()
    queries = sorted({q for p in profiles for q in p.search("queries", [])})
    jobicy_geo = sorted({g for p in profiles for g in p.sources.get("jobicy_geo", [])})
    # Configured sources plus every job board company discovery has found so far.
    jobs, stats = fetch(merge_specs([p.sources for p in profiles] + [store.found_sources()]), queries, jobicy_geo)
    votes.sync(store)
    for p in profiles:
        votes.apply(p, store)
    llm = LLM(args.wait_hours)
    learned = {p.id: {} for p in profiles}
    if not args.no_discovery:
        new_spec, learned = companies.discover(profiles, jobs, store, llm)
        if new_spec:  # boards found just now: searched today already
            more, more_stats = fetch(new_spec)
            jobs, stats = jobs + more, {**stats, **more_stats}
        for p in profiles:
            votes.apply(p, store)  # new company verdicts
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
        run_profile(p, jobs, stats, store, args, llm, learned[p.id])


if __name__ == "__main__":
    main()
