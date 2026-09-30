"""Titles the profile's patterns don't cover: asked once per title in a batch, remembered, and counted as matches."""

from pathlib import Path

from jobagent import connectors, filters, run, titles
from jobagent.profile import Profile
from jobagent.sources import Job
from jobagent.store import Store

EXAMPLE = Path(__file__).parent.parent / "profiles" / "example.toml"


class FakeConn:
    """Keeps every title with "produ" in it (product, produit), numbers as ints or strings; or fails."""

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def chat_json(self, model, system, user, schema):
        self.calls.append(user)
        if self.fail:
            raise self.fail
        lines = [x.split(". ", 1) for x in user.splitlines()]
        return {"fits": [int(n) if i % 2 else n for i, (n, t) in enumerate(lines) if "produ" in t.lower()]}


class FakeLLM:
    def __init__(self, conn):
        self.conn = conn

    def get(self):
        return (self.conn, "m") if self.conn else None


def job(title, **kw):
    return Job("greenhouse", "Acme", title, f"https://x/{title}", location=kw.pop("location", "Lisbon"), **kw)


def test_norm_ignores_case_gender_tags_and_spacing():
    assert titles.norm("Product Owner (m/w/d)") == titles.norm("product  owner (f/m/x)") == "product owner"
    assert titles.norm("Chef de produit H/F") == "chef de produit"
    assert titles.norm("Delivery Lead (all genders) –") == "delivery lead"


def test_only_uncovered_titles_on_otherwise_fitting_jobs_are_asked():
    p = Profile.load(EXAMPLE)
    assert filters.worth_asking(p, job("Product Owner"), {})
    assert not filters.worth_asking(p, job("Junior Product Owner"), {})  # [titles] exclude
    assert not filters.worth_asking(p, job("Product Owner", location="New York"), {})  # other rules
    assert not filters.worth_asking(p, job("Product Owner"), {"product owner": False})  # decided before
    p.learn_titles = False
    assert not filters.worth_asking(p, job("Product Owner"), {})


def test_accepted_titles_count_as_matches_and_are_remembered(tmp_path):
    p, store = Profile.load(EXAMPLE), Store(tmp_path / "agent.sqlite3")
    unknown = {titles.norm(t): [job(t)] for t in ["Product Owner (m/w/d)", "Chef de produit", "Office Manager"]}
    learned = store.title_verdicts(p.id, titles.fingerprint(p))
    accepted, note = run.learn_titles(p, unknown, learned, store, FakeLLM(FakeConn()))
    assert accepted == ["Product Owner (m/w/d)", "Chef de produit"] and not note
    assert filters.title_ok(p, "Product Owner (f/m/x)", learned)
    assert not filters.title_ok(p, "Office Manager", learned)
    assert store.title_verdicts(p.id, titles.fingerprint(p)) == learned
    p.candidate += " Now also open to Porto."
    assert store.title_verdicts(p.id, titles.fingerprint(p)) == {}  # new description, new verdicts


def test_titles_go_in_batches_and_a_failed_batch_stays_undecided(tmp_path):
    p, store = Profile.load(EXAMPLE), Store(tmp_path / "agent.sqlite3")
    many = {f"role {i}": [job(f"Role {i}")] for i in range(titles.BATCH + 5)}
    conn = FakeConn()
    run.learn_titles(p, many, {}, store, FakeLLM(conn))
    assert len(conn.calls) == 2
    bad = FakeConn(fail=connectors.BadOutput("garbled"))
    learned = {}
    assert run.learn_titles(p, {"product owner": [job("Product Owner")]}, learned, store, FakeLLM(bad)) == ([], "")
    assert learned == {}
    assert run.learn_titles(p, {"x": [job("X")]}, {}, store, FakeLLM(None))[1].startswith("no LLM")
