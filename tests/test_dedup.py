"""Copies of the same opening across sources become one job with an id that survives later runs."""

import sqlite3
import time
from contextlib import contextmanager

from jobagent import dedup
from jobagent.dedup import AliasBook, company_match, company_tokens, dedupe, title_canon
from jobagent.sources import Job

TEXT = (
    "You will run our customers' cloud platforms, automate their infrastructure and help their teams ship safely. "
    "You bring several years of hands-on experience with cloud infrastructure, infrastructure as code, CI/CD "
    "pipelines and cost control, and you enjoy explaining complex topics simply to engineers and managers alike. "
    "The contract is flexible and remote; you plan your own week and meet the client team twice a month."
)
OTHER_TEXT = (
    "Our payments team owns card acquiring, settlement and reconciliation for millions of merchants. As a backend "
    "engineer you design resilient services in Go and Kotlin, run them on Kubernetes and work closely with risk and "
    "compliance. We value clear writing, careful code review and on-call ownership of what you ship to production."
)


def job(company, title, source="himalayas", text=TEXT, **kw):
    return Job(source, company, title, f"https://{source}.example/{abs(hash((company, title)))}", text=text, **kw)


def test_title_canon_drops_decorations_but_keeps_meaning():
    assert title_canon("Entwickler:in Backend (m/w/d) - 100% remote") == "entwickler backend"
    assert title_canon("Berater/-in IT (w/m/d)") == "berater it"
    assert title_canon("Engineering Manager | Germany | Remote") == "engineering manager"
    assert title_canon("Cloud Engineer - Contractor (US Canada, Europe, MENA, India & APAC Timezones)") == (
        "cloud engineer contractor"
    )
    assert title_canon("Cloud Engineer - Contractor (US Canada, Europe, MENA, I") == "cloud engineer contractor"
    assert title_canon("Senior Backend Engineer (Payments)") == "senior backend engineer payments"
    assert title_canon("Head of Engineering - Berlin") == "head of engineering berlin"  # places can differ per job


def test_company_names():
    def m(a, b):
        return company_match(company_tokens(a), company_tokens(b))

    assert company_tokens("ACME Software GmbH & Co. KG") == ("acme", "software")
    assert m("ibm", "International Business Machines") == "strong"
    assert m("Grafana", "Grafana Labs GmbH") == "strong"
    assert m("OLIVER", "OLIVER Agency") == "strong"
    assert m("Hero", "Delivery Hero") == "weak"  # could be another company: descriptions must agree
    assert m("meta", "metabase") == "weak"
    assert m("Acme", "Globex") == ""


def test_copies_across_sources_merge_and_the_employer_posting_wins():
    direct = job(
        "Globex", "Cloud Engineer - Contractor (US Canada, Europe, MENA, India & APAC Timezones)", "greenhouse"
    )
    direct.direct = True
    cut = job("Globex", "Cloud Engineer - Contractor (US Canada, Europe, MENA, I", salary_min=40, currency="USD")
    out = dedupe([cut, direct, job("ibm", "Analyst (m/w/d)"), job("International Business Machines", "Analyst")])
    assert len(out) == 2
    best = next(j for j in out if j.copies == 2 and "Cloud" in j.title)
    assert best is direct and best.salary_min == 40  # gaps filled from the other copy
    assert cut.raw_key in best.aliases and best.key == direct.raw_key


def test_similar_titles_merge_only_across_sources_with_the_same_description():
    a = job("Globex", "Independent Contractor - Consultant role for Software Architecture practice", "greenhouse")
    b = job("Globex", "External Contractor - Consultant role for Software Architecture practice", "himalayas")
    assert len(dedupe([a, b])) == 1
    # One board never lists a job twice: similar titles with shared boilerplate there are different jobs.
    same_board = job("Globex", "Independent Contractor - Consultant role for Data Architecture practice", "greenhouse")
    assert len(dedupe([a, same_board])) == 2
    assert (
        len(
            dedupe(
                [
                    job("Acme", "Senior Backend Engineer", "greenhouse", text=OTHER_TEXT),
                    job("Acme", "Backend Engineer", "himalayas", text=TEXT),
                ]
            )
        )
        == 2
    )
    # Same title at "Hero" and "Delivery Hero": only one company if the postings (from different sources) agree.
    hero = job("Delivery Hero", "Backend Engineer", "greenhouse")
    assert len(dedupe([job("Hero", "Backend Engineer", text=OTHER_TEXT), hero])) == 2
    assert len(dedupe([job("Hero", "Backend Engineer"), hero])) == 1
    assert len(dedupe([job("Hero", "Backend Engineer", "greenhouse"), hero])) == 2


def test_countries_in_titles_are_different_jobs():
    assert (
        len(
            dedupe(
                [
                    job("Acme", "Sales Engineer - Spain", "greenhouse"),
                    job("Acme", "Sales Engineer - Germany", "greenhouse"),
                ]
            )
        )
        == 2
    )
    assert (
        len(dedupe([job("Acme", "Sales Engineer - Spain", "greenhouse"), job("Acme", "Sales Engineer - Spain")])) == 1
    )


def _book(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS sent (k TEXT)")
    dedup.create_alias_table(conn, "SELECT k AS k FROM sent")

    @contextmanager
    def connect():
        yield conn
        conn.commit()

    return conn, AliasBook(connect)


def test_ids_survive_when_the_first_copy_disappears(tmp_path):
    _, book = _book(tmp_path / "a.sqlite3")
    agg = job("Acme GmbH", "Data Engineer (m/w/d)")
    direct = job("ACME", "Data Engineer", "greenhouse", direct=True)
    first = dedupe([agg, direct], book)[0]
    # Next run: the employer's posting is gone, only an aggregator copy is left – same id, so no second email.
    later = dedupe([job("Acme GmbH", "Data Engineer (m/w/d)")], book)[0]
    assert later.key == first.key == direct.raw_key


def test_existing_keys_keep_their_meaning_when_the_alias_table_is_created(tmp_path):
    conn = sqlite3.connect(tmp_path / "b.sqlite3")
    old = job("Acme GmbH", "Data Engineer (m/w/d)")
    conn.execute("CREATE TABLE sent (k TEXT)")
    conn.execute("INSERT INTO sent VALUES (?)", (old.raw_key,))
    dedup.create_alias_table(conn, "SELECT k AS k FROM sent")

    @contextmanager
    def connect():
        yield conn

    # The employer's copy shows up for the first time: it must take over the id the old copy was stored under.
    out = dedupe([job("ACME", "Data Engineer", "greenhouse", direct=True), old], AliasBook(connect))
    assert out[0].key == old.raw_key


def test_read_only_book_changes_nothing(tmp_path):
    conn, book = _book(tmp_path / "c.sqlite3")
    book.read_only = True
    dedupe([job("Acme", "Data Engineer")], book)
    assert conn.execute("SELECT COUNT(*) FROM job_alias").fetchone()[0] == 0


def test_fast_enough_for_large_pools():
    jobs = [
        job(f"Company {i % 3000}", f"Role {i % 97} Engineer {i}", text=f"{TEXT} {i}", source="arbeitnow")
        for i in range(30000)
    ]
    start = time.time()
    assert len(dedupe(jobs)) == 30000
    assert time.time() - start < 10
