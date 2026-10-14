import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from jobagent.sources import Job

import app as webapp
import matching
import pages


def job(title, location, company="Acme", remote=False, partner="", day=1):
    posted = datetime(2026, 10, day, tzinfo=UTC)
    return Job("greenhouse", company, title, f"https://jobs.example/{title}/{location}", location=location,
               remote=remote, partner=partner, posted=posted, direct=not partner)  # fmt: skip


POOL = [
    job("Senior Product Manager", "Berlin, Germany", day=3),
    job("Product Owner", "Remote - Europe", company="Beta", remote=True, day=5),
    job("Product Manager", "Remote - US", company="Gamma", remote=True),  # remote, but not from Germany
    job("Junior Product Manager", "Munich, Germany", company="Delta"),  # too junior for the page's search
    job("Product Marketing Manager", "Hamburg", company="Epsilon"),  # another field
    job("Product Manager", "Berlin", company="Jooble Co", partner="Jooble"),  # partner links stay in emails
    job("Engineering Manager", "München", company="Zeta"),
]


@pytest.fixture
def snapshot(monkeypatch):
    monkeypatch.setitem(pages.SETTINGS, "min_jobs", 1)
    pages.save(matching.TitleIndex(POOL), min_score=70)
    yield
    pages.SNAPSHOT.unlink(missing_ok=True)


def test_page_lists_what_the_digest_would_send_newest_first(snapshot):
    p = pages.get("product-manager", "germany", "de")
    assert p["role"] == "Produktmanager" and p["place"] == "Deutschland"
    assert [j["title"] for j in p["jobs"]] == ["Product Owner", "Senior Product Manager"]
    assert p["count"] == 2 and p["jobs"][0]["posted"] == "2026-10-05"
    assert [j["company"] for j in pages.get("engineering-manager", "germany", "en")["jobs"]] == ["Zeta"]


def test_no_page_when_unknown_too_few_jobs_or_stale(snapshot, monkeypatch):
    assert pages.get("product-manager", "spain", "en") is None
    assert pages.get("nurse", "germany", "en") is None
    monkeypatch.setitem(pages.SETTINGS, "min_jobs", 3)
    assert pages.get("product-manager", "germany", "en") is None
    monkeypatch.setitem(pages.SETTINGS, "min_jobs", 1)
    pages.save(matching.TitleIndex(POOL), min_score=70, now=time.time() - 4 * 86400)
    assert pages.get("product-manager", "germany", "en") is None


def test_job_page_shows_jobs_and_starts_the_form_with_the_search(snapshot):
    with TestClient(webapp.app) as c:
        r = c.get("/de/jobs/product-manager/germany")
        assert r.status_code == 200
        assert "Produktmanager-Jobs in" in r.text and "Senior Product Manager" in r.text
        assert 'href="https://jobs.example/Product Owner/Remote - Europe" rel="nofollow noopener"' in r.text
        assert "Ich suche eine Stelle als Produktmanager" in r.text  # the form's text, ready to edit
        assert '<link rel="canonical" href="https://jobs.example.com/de/jobs/product-manager/germany">' in r.text
        assert "/de/jobs/product-manager/germany" in c.get("/sitemap.xml").text
        missing = c.get("/en/jobs/product-manager/spain")
        assert missing.status_code == 404 and 'name="robots" content="noindex"' in missing.text
