"""The job agent learns: signed 👍/👎 links from its digest, votes that become rules and model examples, and company
discovery (similar employers → their job boards)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobagent import companies, filters, report, run, sources, votes
from jobagent.profile import Profile
from jobagent.sources import Job
from jobagent.store import Store

import app as webapp

EXAMPLE = Path(__file__).parent.parent / "profiles" / "example.toml"


def job(title="Senior Product Manager", company="Acme GmbH", **kw):
    return Job("greenhouse", company, title, f"https://x/{title}", location=kw.pop("location", "Lisbon"), **kw)


@pytest.fixture
def client():
    with TestClient(webapp.app) as c:
        yield c


# ---------- signed links and the web app ----------


def test_tokens_carry_the_job_and_cannot_be_forged():
    tok = votes.token("prof1", "acme|pm", "Product Manager (m/w/d)", "Acme")
    assert votes.verify(tok) == {"p": "prof1", "k": "acme|pm", "t": "Product Manager (m/w/d)", "c": "Acme"}
    payload, sig = tok[2:].rsplit(".", 1)
    forged = votes._b64(b'{"p":"prof1","k":"x","t":"x","c":"x"}')
    assert votes.verify(f"a.{forged}.{sig}") is None
    assert votes.verify(f"a.{payload}.{sig[:-1]}A") is None or sig.endswith("A")
    assert votes.verify("random-web-token") is None


def test_digest_links_vote_through_the_web_app_and_the_agent_pulls_them(client, tmp_path, monkeypatch):
    p, s = Profile.load(EXAMPLE), Store(tmp_path / "agent.sqlite3")
    links = votes.links(p.id, job())
    assert links["up"].startswith("https://jobs.example.com/feedback/a.")
    path = links["down"].removeprefix("https://jobs.example.com")
    page = client.get(path)
    assert page.status_code == 200 and "Senior Product Manager" in page.text  # GET only shows the page
    assert client.get("/agent-feedback", headers={"Authorization": f"Bearer {votes.api_key()}"}).json() == {"votes": []}
    tok = path.split("/feedback/")[1].split("?")[0]
    r = client.post(f"/feedback/{tok}", data={"vote": "down", "reason": ["not this company", "<b>"]})
    assert r.status_code == 200
    assert client.get("/agent-feedback").status_code == 403
    assert client.get("/agent-feedback", headers={"Authorization": "Bearer wrong"}).status_code == 403

    class Resp:  # the agent's request, answered by the test client
        def __init__(self, r):
            self.r = r

        def raise_for_status(self):
            assert self.r.status_code == 200

        def json(self):
            return self.r.json()

    monkeypatch.setattr(
        votes.requests,
        "get",
        lambda url, params, headers, timeout: Resp(client.get("/agent-feedback", params=params, headers=headers)),
    )
    assert votes.sync(s) == 1
    [v] = s.votes(p.id)
    assert (v["title"], v["company"], v["vote"], v["reason"], v["origin"]) == (
        "Senior Product Manager",
        "Acme GmbH",
        "down",
        "not this company",
        "email",
    )


# ---------- what votes teach ----------


def test_votes_become_rules_and_examples(tmp_path):
    p, s = Profile.load(EXAMPLE), Store(tmp_path / "agent.sqlite3")
    s.set_vote(p.id, "k1", "Senior Product Manager", "Acme GmbH", "down", "not this company", "chat")
    s.set_vote(p.id, "k2", "Senior Product Manager, Payments", "Beta", "down", "not my field; other", "chat")
    s.set_vote(p.id, "k3", "Product Owner (m/w/d)", "Gamma SL", "up", "", "email")
    votes.apply(p, s)
    assert filters.prefilter(p, job()) == "company"  # Acme blocked
    assert not filters.title_ok(p, "Senior Product Manager, Payments")  # included by the pattern, voted down
    assert filters.title_ok(p, "Product Owner (f/m/x)")  # not in the patterns, voted up
    assert "gamma" in p.liked_companies and "acme" not in p.liked_companies
    assert "DISLIKED: Senior Product Manager – Acme GmbH (not this company)" in p.examples_block()
    assert "LIKED: Product Owner (m/w/d) – Gamma SL" in p.examples


def test_your_company_verdict_beats_the_models(tmp_path):
    p, s = Profile.load(EXAMPLE), Store(tmp_path / "agent.sqlite3")
    s.set_company_verdict(p.id, "acme", "Acme", False, "you")
    s.set_company_verdict(p.id, "acme", "Acme", True, "model")
    assert s.company_verdicts(p.id)["acme"] == {"name": "Acme", "fits": False, "origin": "you"}
    votes.apply(p, s)
    assert "acme" in p.blocked_companies


def test_digest_shows_ids_vote_buttons_and_what_was_learned():
    p = Profile.load(EXAMPLE)
    j = job()
    j.assessment, j.score = {"total": 80, "one_line": "fits"}, 80
    html, text = report.render(
        p,
        {"strong": [j], "possible": [], "contract": []},
        [],
        [],
        {},
        10,
        1,
        "",
        learned={"titles": ["Product Owner"], "found": ["GFT (successfactors)"], "rated": []},
    )
    sid = votes.short_id(p.id, j.key)
    assert f"id {sid}" in text and "👍 https://jobs.example.com/feedback/a." in text
    assert "more like this" in html and "GFT (successfactors)" in html and "Similar companies" not in text


# ---------- company discovery ----------


def test_board_links_and_guesses():
    page = """<a href="https://boards.greenhouse.io/acme">Jobs</a> <script src="https://boards.greenhouse.io/embed/
    job_board/js?for=beta"></script> <a href="https://acme.wd3.myworkdayjobs.com/en-US/External">x</a>
    <a href="https://apply.workable.com/j/ABC123">one job</a> <a href="https://gamma.jobs.personio.de/">p</a>"""
    assert companies.board_links(page) == [
        ("greenhouse", "acme"),
        ("personio", "gamma"),
        ("workday", "acme/wd3/External"),
    ]
    assert companies.guesses("Plain Concepts S.L.") == ["plainconcepts", "plain-concepts"]
    assert companies.guesses("GFT Technologies SE") == ["gfttechnologies", "gft-technologies", "gft"]


def test_guessed_boards_must_list_jobs_and_name_the_company(monkeypatch):
    boards = {("workable", "gft"): (True, "GFT", 0), ("lever", "acme"): (True, "", 3)}
    boards[("greenhouse", "beta")] = (True, "Beta Bank Ltd", 12)  # another company with the same short name
    monkeypatch.setattr(companies, "probe", lambda ats, slug: boards.get((ats, slug), (False, "", 0)))
    assert companies.resolve("GFT") is None  # an empty account is no job board
    assert companies.resolve("Acme") == ("lever", "acme")
    boards[("greenhouse", "delta")] = (True, "Delta GmbH", 5)
    assert companies.resolve("Beta") is None  # the board belongs to "Beta Bank"
    assert companies.resolve("Delta") == ("greenhouse", "delta")
    assert "systems" not in companies.guesses("T-Systems Iberia")
    assert "t-systemsiberia" in companies.guesses("T-Systems Iberia")


class RateConn:
    """Keeps every company with "Consult" in its line; suggests two companies, one without a usable website."""

    def chat_json(self, model, system, user, schema):
        if "companies" in schema["properties"]:
            return {"companies": [{"name": "Nearshore Co", "website": "https://nearshore.example"}, {"name": "X"}]}
        return {"fits": [int(line.split(".")[0]) for line in user.splitlines() if "Consult" in line]}


def test_discovery_rates_suggests_resolves_and_reports(tmp_path, monkeypatch):
    p, s = Profile.load(EXAMPLE), Store(tmp_path / "agent.sqlite3")
    p.raw["companies"] = {"like": ["Acme Consulting"]}
    p.like_companies = ["Acme Consulting"]
    votes.apply(p, s)
    agg = [
        Job("remotive", "Delta Consulting", "PM", "https://r/1", location="Lisbon", text="We consult banks."),
        Job("remotive", "Pizza Place", "Chef", "https://r/2", location="Lisbon", text="Pizza."),
        Job("remotive", "Far Consulting", "PM", "https://r/3", location="Tokyo", text="Consult."),  # can't work there
    ]
    looked_up = []

    def fake_resolve(name, website=""):
        looked_up.append((name, website))
        return ("workable", "deltaconsulting") if name == "Delta Consulting" else None

    monkeypatch.setattr(companies, "resolve", fake_resolve)
    spec, rep = companies.discover([p], agg, s, run_llm(RateConn()))
    assert spec == {"workable": ["deltaconsulting"]}
    assert rep[p.id]["rated"] == ["Delta Consulting"]
    assert rep[p.id]["suggested"] == ["Nearshore Co", "X"]
    assert rep[p.id]["found"] == ["Delta Consulting (workable)"]
    assert rep[p.id]["unreadable"] == ["Nearshore Co (https://nearshore.example)"]
    assert ("Nearshore Co", "https://nearshore.example") in looked_up
    assert s.found_sources() == {"workable": ["deltaconsulting"]}
    assert s.company_verdicts(p.id)["pizzaplace"]["fits"] is False
    assert "farconsulting" not in s.company_verdicts(p.id)
    # Next run: nothing is asked or looked up twice.
    looked_up.clear()
    spec, rep = companies.discover([p], agg, s, run_llm(RateConn()))
    assert spec == {} and rep[p.id]["rated"] == [] and looked_up == []


def run_llm(conn):
    class LLM:
        def get(self):
            return conn, "m"

    return LLM()


def test_profiles_without_wanted_companies_skip_discovery(tmp_path):
    p, s = Profile.load(EXAMPLE), Store(tmp_path / "agent.sqlite3")
    p.like_companies = []
    votes.apply(p, s)
    assert companies.discover([p], [job()], s, run.LLM(0)) == ({}, {p.id: {k: [] for k in run_report_keys()}})


def run_report_keys():
    return ("rated", "suggested", "found", "unreadable")


# ---------- new job boards ----------


def test_workday_dates_and_successfactors_feed(monkeypatch):
    assert sources._workday_posted("Posted Today").date() == sources.datetime.now(sources.UTC).date()
    age = sources.datetime.now(sources.UTC) - sources._workday_posted("Posted 30+ Days Ago")
    assert round(age.total_seconds() / 86400) == 30
    assert sources._workday_posted("") is None
    rss = b"""<?xml version="1.0"?><rss><channel><item><title>Delivery Lead (Alcobendas, M, ES, 28108)</title>
    <description>&lt;p&gt;Lead &amp;amp; deliver&lt;/p&gt;</description><pubDate>Wed, 30 Sep 2026 0:00:00 GMT</pubDate>
    <link>https://jobs.gft.com/job/x/1/?feedId=null&amp;utm_source=J2WRSS</link></item></channel></rss>"""

    class R:
        content = rss

        def raise_for_status(self):
            pass

    monkeypatch.setattr(sources.requests, "get", lambda *a, **kw: R())
    [j] = sources.successfactors("jobs.gft.com")
    assert (j.company, j.title, j.location, j.url) == (
        "gft",
        "Delivery Lead",
        "Alcobendas, M, Spain",
        "https://jobs.gft.com/job/x/1/",
    )
    assert j.text == "Lead & deliver" and j.direct and j.posted.day == 30
