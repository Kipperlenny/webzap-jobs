"""Votes and company verdicts from the command line – the same as the digest's 👍/👎 buttons, e.g. from a chat with
a coding assistant that runs this on the server.

    python3 -m jobagent.feedback list [-p PROFILE] [--days 14]       jobs sent recently, with their ids and votes
    python3 -m jobagent.feedback vote ID up|down [-r REASON ...]     REASON: field location junior senior salary
                                                                      company known other
    python3 -m jobagent.feedback company like|block|forget NAME -p PROFILE
    python3 -m jobagent.feedback companies [-p PROFILE]              wanted companies and their job boards
    python3 -m jobagent.feedback sync                                pull the votes made in emails now

PROFILE is the profile file's name without .toml (e.g. "lennart"); without -p, list/companies show all profiles.
"""

import argparse
import sys

from . import votes
from .common import load_env
from .config import ENV_FILES, EXAMPLE_PROFILE, PRIVATE_PROFILES
from .dedup import company_key
from .profile import discover
from .store import Store


def _profiles(stem: str | None):
    ps = discover([], PRIVATE_PROFILES, EXAMPLE_PROFILE)
    if stem:
        ps = [p for p in ps if p.path.stem == stem]
        if not ps:
            sys.exit(f"no profile named {stem!r}")
    return ps


def find_job(store, profiles, short_id: str):
    """(profile, match row) for a digest id, searched in every job the agent has judged."""
    for p in profiles:
        for key, title, company in store.db.execute("SELECT key, title, company FROM matches WHERE profile=?", (p.id,)):
            if votes.short_id(p.id, key) == short_id:
                return p, {"key": key, "title": title, "company": company}
    return None, None


def cmd_list(store, profiles, days):
    for p in profiles:
        voted = {v["key"]: v for v in store.votes(p.id)}
        print(f"== {p.path.stem} – {p.name}")
        for j in store.emailed(p.id, days):
            v = voted.get(j["key"])
            mark = f"  [{'👍' if v['vote'] == 'up' else '👎'} {v['reason']}]".rstrip() if v else ""
            print(
                f"  {votes.short_id(p.id, j['key'])}  {j['emailed_at'][:10]}  {j['total'] or '–':>3}  "
                f"{j['title']} – {j['company']}{mark}"
            )


def cmd_vote(store, profiles, short_id, vote, reasons):
    p, job = find_job(store, profiles, short_id)
    if not job:
        sys.exit(f"no job with id {short_id}")
    unknown = [r for r in reasons if r not in votes.REASONS]
    if unknown:
        sys.exit(f"unknown reason(s) {unknown}; use {sorted(votes.REASONS)}")
    reason = "; ".join(votes.REASONS[r] for r in reasons)
    store.set_vote(p.id, job["key"], job["title"], job["company"], vote, reason, "chat")
    store.commit()
    mark = "👍" if vote == "up" else "👎"
    print(f"{mark} {job['title']} – {job['company']} ({p.path.stem})" + (f" – {reason}" if reason else ""))


def cmd_company(store, profiles, action, name):
    if len(profiles) != 1:
        sys.exit("name the profile with -p")
    p, k = profiles[0], company_key(name)
    if action == "forget":
        store.forget_company_verdict(p.id, k)
    else:
        store.set_company_verdict(p.id, k, name, action == "like", "you")
        if action == "like":
            store.add_company(k, name, origin="liked")
    store.commit()
    print(f"{action}: {name} ({p.path.stem})")


def cmd_companies(store, profiles):
    boards = {c["key"]: c for c in store.companies()}
    for p in profiles:
        votes.apply(p, store)
        verdicts = store.company_verdicts(p.id)
        print(f"== {p.path.stem} – {p.name}")
        for k, name in sorted(p.liked_companies.items(), key=lambda kv: kv[1].lower()):
            c = boards.get(k) or {}
            board = f"{c['ats']}:{c['slug']}" if c.get("status") == "found" else c.get("status", "not looked up")
            origin = verdicts.get(k, {}).get("origin", "profile / 👍")
            print(f"  + {name:40} {board:40} ({origin})")
        for k in sorted(p.blocked_companies):
            print(f"  - {verdicts.get(k, {}).get('name', k)}")


def main():
    common = argparse.ArgumentParser(add_help=False)  # -p works before or after the command
    common.add_argument("-p", "--profile", help="profile file name without .toml")
    ap = argparse.ArgumentParser(description="Votes and company verdicts for the job agent", parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", parents=[common])
    ls.add_argument("--days", type=int, default=14)
    v = sub.add_parser("vote", parents=[common])
    v.add_argument("id")
    v.add_argument("vote", choices=["up", "down"])
    v.add_argument("-r", "--reason", action="append", default=[])
    c = sub.add_parser("company", parents=[common])
    c.add_argument("action", choices=["like", "block", "forget"])
    c.add_argument("name")
    sub.add_parser("companies", parents=[common])
    sub.add_parser("sync", parents=[common])
    args = ap.parse_args()
    load_env(ENV_FILES)
    store, profiles = Store(), _profiles(args.profile)
    if args.cmd == "list":
        cmd_list(store, profiles, args.days)
    elif args.cmd == "vote":
        cmd_vote(store, profiles, args.id, args.vote, args.reason)
    elif args.cmd == "company":
        cmd_company(store, profiles, args.action, args.name)
    elif args.cmd == "companies":
        cmd_companies(store, profiles)
    else:
        print(f"{votes.sync(store)} new vote(s)")


if __name__ == "__main__":
    main()
