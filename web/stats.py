"""Plain numbers for the operator: ad campaigns, link clicks and sponsored jobs. Nothing here identifies a person.

docker exec webzap-jobs-web python stats.py            # last 30 days
docker exec webzap-jobs-web python stats.py --days 7
"""

import argparse

import store


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=30)
    days = ap.parse_args().days
    rows = store.campaign_report(days)
    print(f"Ad campaigns, last {days} days (links with ?c=<name>):")
    print(f"  {'campaign':<24}{'visits':>8}{'sign-ups':>10}{'confirmed':>11}{'rate':>7}")
    for name, visits, signups, confirmed in rows:
        rate = f"{confirmed / visits:.0%}" if visits else "–"
        print(f"  {name:<24}{visits:>8}{signups:>10}{confirmed:>11}{rate:>7}")
    if not rows:
        print("  none yet")
    print(f"\nLink clicks in our emails, last {days} days (per kind and source, no persons):")
    clicks = store.click_report(days)
    for kind, source, n in clicks:
        print(f"  {kind:<10}{source:<34}{n:>8}")
    if not clicks:
        print("  none yet")
    print("\nSponsored jobs (all time):")
    sponsored = store.sponsored_stats()
    for sponsor_id, s in sponsored.items():
        print(f"  {sponsor_id}: sent {s['sent']}, 👍 {s['up'] or 0}, 👎 {s['down'] or 0}")
    if not sponsored:
        print("  none yet")


if __name__ == "__main__":
    main()
