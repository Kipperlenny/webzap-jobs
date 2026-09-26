"""Links in emails are counted per source through /go – never per person, and /go is no open redirect."""

from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from jobagent.sources import Job

import app
import golinks
import mailer
import store
from matching import Match

client = TestClient(app.app, follow_redirects=False)


def params(link):
    return {k: v[0] for k, v in parse_qs(urlparse(link).query).items()}


def clicks(kind, source):
    return dict(((k, s), n) for k, s, n in store.click_report(1)).get((kind, source), 0)


def test_signed_link_forwards_and_counts_one_click():
    link = golinks.link("https://www.stepstone.de/jobs/data-engineer", "external", "stepstone-de")
    assert link.startswith("https://jobs.example.com/go?")
    before = clicks("external", "stepstone-de")
    r = client.get(link.replace("https://jobs.example.com", ""))
    assert r.status_code == 302 and r.headers["location"] == "https://www.stepstone.de/jobs/data-engineer"
    assert r.headers["x-robots-tag"] == "noindex, nofollow"
    assert clicks("external", "stepstone-de") == before + 1
    client.head(link.replace("https://jobs.example.com", ""))  # link scanners checking the link don't count
    assert clicks("external", "stepstone-de") == before + 1


def test_no_open_redirect_and_no_forged_sources():
    p = params(golinks.link("https://good.example/job", "job", "greenhouse"))
    for forged in ({**p, "u": "https://evil.example/"}, {**p, "s": "other"}, {**p, "k": "sponsored"}, {**p, "h": "0"}):
        assert client.get("/go", params=forged).status_code == 404
    assert client.get("/go", params={"u": "https://evil.example/"}).status_code == 404
    assert golinks.link("#", "job", "x") == "#"  # sample email placeholders stay as they are


def test_email_links_carry_only_kind_and_source():
    m = Match(80, "", "PM", "remote", "worldwide")
    item = mailer.digest_item(Job("jooble", "Acme", "PM", "https://j.example/1", partner="Jooble"), m, "en", "f")
    assert params(item["url"])["k"] == "partner" and params(item["url"])["s"] == "jooble"
    sp = mailer.digest_item(Job("sponsored", "Acme", "PM", "https://a.example/2"), m, "en", "f", sponsor_id="Acme Q4")
    assert params(sp["url"])["k"] == "sponsored" and params(sp["url"])["s"] == "acme-q4"
    ext = mailer.external_item({"id": "stepstone-de", "name": "StepStone", "url": "https://s.example/x"})
    assert set(params(ext["url"])) == {"u", "k", "s", "h"}  # nothing about the recipient


def test_stats_are_capped_per_day(monkeypatch):
    monkeypatch.setattr(store, "MAX_CLICK_SOURCES", 0)
    before = clicks("job", "brand-new-source")
    store.count_click("job", "brand-new-source")
    assert clicks("job", "brand-new-source") == before == 0
