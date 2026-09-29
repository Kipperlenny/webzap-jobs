import json
import re

import pytest
from fastapi.testclient import TestClient

from jobagent.sources import Job

import app as webapp
import store
from matching import Match


@pytest.fixture
def client(monkeypatch):
    sent = []
    for kind in ("confirmation", "welcome", "manage_link"):
        monkeypatch.setattr(
            webapp.mailer, f"send_{kind}", lambda to, data, lang, kind=kind: sent.append((kind, to, data, lang))
        )
    webapp._hits.clear()
    with TestClient(webapp.app) as c:
        c.sent = sent
        yield c


def test_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in r.headers


def test_signup_validation_and_confirmation_mail(client):
    assert client.post("/signup", data={"email": "bad", "wish": "short"}).status_code == 400
    r = client.post(
        "/signup", data={"email": "new@example.com", "wish": "I want to lead a nursing team in Madrid.", "consent": "1"}
    )
    assert r.status_code == 200 and client.sent[0][:2] == ("confirmation", "new@example.com")


def test_honeypot_pretends_success_without_mail(client):
    r = client.post("/signup", data={"email": "bot@example.com", "wish": "x" * 40, "consent": "1", "website": "spam"})
    assert r.status_code == 200 and not client.sent


def test_token_pages_404_and_noindex(client):
    r = client.get("/manage/not-a-token")
    assert r.status_code == 404
    assert r.headers["x-robots-tag"] == "noindex, nofollow"


def test_feedback_get_does_not_vote_but_post_does(client):
    t = store.add_pending("vote@example.com", "I want to test votes properly.", "daily")
    store.confirm(t["confirm"])
    tok = store.record_sent(
        store.signup_id_by_manage(t["manage"]), Job("greenhouse", "Acme", "PM", "https://x/2"), Match(80, "r", "PM")
    )
    assert client.get(f"/feedback/{tok}?v=down").status_code == 200
    assert store.feedback_get(tok)["feedback"] is None
    assert (
        client.post(f"/feedback/{tok}", data={"vote": "down", "reason": ["too junior", "<script>"]}).status_code == 200
    )
    assert store.feedback_get(tok) == {**store.feedback_get(tok), "feedback": "down", "feedback_reason": "too junior"}


def test_one_click_unsubscribe(client):
    t = store.add_pending("unsub@example.com", "I want to test unsubscribing.", "daily")
    store.confirm(t["confirm"])
    r = client.post(f"/unsubscribe/{t['manage']}", data={"List-Unsubscribe": "One-Click"})
    assert r.status_code == 200
    assert store.get_by_manage(t["manage"])["frequency"] == "paused"


def test_language_from_url_prefix_or_browser(client):
    de = client.get("/de/")
    assert '<html lang="de">' in de.text and "vary" not in de.headers
    es = client.get("/", headers={"Accept-Language": "es-ES,es;q=0.9,en;q=0.5"})
    assert '<html lang="es">' in es.text and es.headers["vary"] == "Accept-Language"
    assert '<html lang="en">' in client.get("/", headers={"Accept-Language": "ja"}).text
    assert 'href="/fr/privacy"' in client.get("/fr/").text  # links keep the language


def test_signup_stores_language_and_welcome_mail_after_confirm(client):
    client.post("/fr/signup", data={"email": "fr@example.com", "wish": "Je veux travailler à Lyon.", "consent": "1"})
    kind, to, tokens, lang = client.sent[-1]
    assert (kind, lang) == ("confirmation", "fr")
    assert client.post(f"/fr/confirm/{tokens['confirm']}").status_code == 200
    assert client.sent[-1][:2] == ("welcome", "fr@example.com") and client.sent[-1][3] == "fr"
    assert client.sent[-1][2] == tokens["manage"]
    assert store.get_by_manage(tokens["manage"])["lang"] == "fr"


def test_campaign_counts_are_aggregate_and_leave_no_trace(client):
    before = dict((r[0], r[1:]) for r in store.campaign_report()).get("gads-de", (0, 0, 0, 0))
    r = client.get("/de/?c=GAds-DE&gclid=abc")
    assert 'name="campaign" value="gads-de"' in r.text and "abc" not in r.text
    client.get("/?c=<script>")  # invalid names are ignored
    data = {"email": "ad@example.com", "wish": "Ich suche einen Job in Berlin.", "consent": "1", "campaign": "gads-de"}
    client.post("/de/signup", data=data)
    tokens = client.sent[-1][2]
    client.post(f"/confirm/{tokens['confirm']}")
    after = dict((r[0], r[1:]) for r in store.campaign_report())["gads-de"]
    assert tuple(a - b for a, b in zip(after, before, strict=True)) == (1, 1, 1, 0)
    assert all(not r[0].startswith("<") for r in store.campaign_report())
    with store._conn() as c:  # the campaign name is gone from the entry once it is confirmed
        assert c.execute("SELECT COUNT(*) FROM signups WHERE campaign IS NOT NULL").fetchone()[0] == 0


def _campaign(name):
    return dict((r[0], r[1:]) for r in store.campaign_report()).get(name, (0, 0, 0, 0))


@pytest.mark.parametrize(
    "ua",
    [
        "AdsBot-Google (+http://www.google.com/adsbot.html)",
        "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/99.0.4844.84 Mobile Safari/537.36 (compatible; AdsBot-Google-Mobile; +http://www.google.com/mobile/adsbot.html)",
        "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "curl/8.5.0",
        "",
    ],
)
def test_crawlers_are_not_counted_as_campaign_visits(client, ua):
    before = _campaign("gads-bots")
    r = client.get("/de/?c=gads-bots", headers={"user-agent": ua})
    assert 'name="campaign" value="gads-bots"' in r.text  # the page itself is the same
    assert _campaign("gads-bots") == before


def test_real_browsers_are_counted_as_campaign_visits(client):
    before = _campaign("gads-people")
    for ua in (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.0 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 "
        "Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0",
    ):
        client.get("/de/?c=gads-people", headers={"user-agent": ua})
    assert _campaign("gads-people")[0] == before[0] + 3


def test_failed_signup_attempts_are_counted_per_campaign(client, monkeypatch):
    before = _campaign("gads-fail")
    bad = {"email": "not-an-email", "wish": "Ich suche einen Job in Berlin.", "consent": "1", "campaign": "gads-fail"}
    assert client.post("/de/signup", data=bad).status_code == 400
    assert client.post("/de/signup", data=bad | {"campaign": ""}).status_code == 400  # no campaign: nothing counted

    def fail(*a):
        raise OSError("smtp down")

    monkeypatch.setattr(webapp.mailer, "send_confirmation", fail)
    good = bad | {"email": "fail@example.com"}
    assert client.post("/de/signup", data=good).status_code == 502
    visits, signups, confirmed, failed = _campaign("gads-fail")
    assert failed == before[3] + 2


def test_sample_email_page_and_its_own_csp(client):
    page = client.get("/es/sample")
    assert page.status_code == 200 and 'src="/es/sample/mail"' in page.text
    mail = client.get("/es/sample/mail")
    assert "style-src 'unsafe-inline'" in mail.headers["content-security-policy"]
    assert "script-src" not in mail.headers["content-security-policy"]  # default-src 'none' covers scripts
    assert "Northbank" in mail.text


def test_start_page_names_the_audience_and_shows_the_sample_jobs(client):
    page = client.get("/de/")
    assert "Tech und Produkt" in page.text and "Staff Backend Engineer" in page.text  # hero and form example
    for company in ("Northbank", "Fieldnote", "Clearpath"):  # the sample email's jobs, before signing up
        assert company in page.text
    assert "Fiktive Beispiele" in page.text and 'href="/de/sample"' in page.text
    assert "Gesponsert" in page.text or "Sponsored" in page.text  # labels shown as in the email


def test_legal_pages_come_from_the_instance_with_fallback_and_binding_note(client):
    en = client.get("/en/privacy")
    assert "English translation" in en.text and 'href="/es/privacy"' in en.text  # says which version is binding
    es = client.get("/es/privacy")
    assert "Texto vinculante" in es.text and "versión original" not in es.text  # the binding one needs no note
    assert "Texto vinculante" in client.get("/de/privacy").text  # no German page: the binding version
    missing = client.get("/fr/imprint")  # the repository ships no legal pages
    assert missing.status_code == 404 and "Cette installation" in missing.text


def test_pages_for_search_engines_and_ai_assistants(client):
    robots = client.get("/robots.txt")
    assert robots.status_code == 200 and "Allow: /" in robots.text
    assert "Sitemap: https://jobs.example.com/sitemap.xml" in robots.text
    sitemap = client.get("/sitemap.xml")
    assert sitemap.headers["content-type"].startswith("application/xml")
    assert "<loc>https://jobs.example.com/de/</loc>" in sitemap.text
    assert "<loc>https://jobs.example.com/es/sample</loc>" in sitemap.text
    assert 'hreflang="x-default" href="https://jobs.example.com/sample"' in sitemap.text
    llms = client.get("/llms.txt")
    assert llms.headers["content-type"].startswith("text/plain")
    assert llms.text.startswith("# WebZap Jobs") and "https://jobs.example.com/de/" in llms.text


def test_google_site_verification_only_for_the_configured_file(client, monkeypatch):
    assert client.get("/google1234abcd5678ef90.html").status_code == 404  # not configured
    monkeypatch.setattr(webapp, "GOOGLE_SITE_VERIFICATION", "google1234abcd5678ef90.html")
    r = client.get("/google1234abcd5678ef90.html")
    assert r.status_code == 200 and r.text == "google-site-verification: google1234abcd5678ef90.html"
    assert client.get("/googleffff.html").status_code == 404


def test_llms_txt_lists_the_configured_sources(client, monkeypatch):
    monkeypatch.setitem(webapp.digest.CONFIG, "sources", {"aggregators": ["remoteok"], "lever": ["a", "b"]})
    monkeypatch.setattr(webapp.feeds, "active", lambda: ["jooble"])
    text = client.get("/llms.txt").text
    assert "Sources: 2 employer career pages" in text
    assert "the job boards Remote OK, and the partner job search engines Jooble." in text
    monkeypatch.setattr(webapp.feeds, "active", lambda: [])
    assert "partner" not in client.get("/llms.txt").text.split("## Facts")[0]


def test_landing_page_has_structured_data_and_link_preview(client):
    html = client.get("/de/").text
    data = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S).group(1))
    assert data["@type"] == "WebApplication" and data["url"] == "https://jobs.example.com/de/"
    assert data["inLanguage"] == "de" and data["offers"]["price"] == "0"
    assert '<meta property="og:url" content="https://jobs.example.com/de/">' in html
    assert '<meta property="og:locale" content="de_DE">' in html
