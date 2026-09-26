"""What we already know about a job is reused: the agent's model assessments, and "already sent" in the digest."""

import random
from pathlib import Path

from jobagent import assess
from jobagent.dedup import dedupe
from jobagent.profile import Profile
from jobagent.sources import Job
from jobagent.store import Store

import digest
import matching
import store
from matching import Match

EXAMPLE = Path(__file__).parent.parent / "profiles" / "example.toml"


def test_agent_finds_an_assessment_stored_under_another_copy(tmp_path):
    s = Store(tmp_path / "agent.sqlite3")
    p = Profile.load(EXAMPLE)
    agg = Job("himalayas", "Acme GmbH", "Product Manager (m/w/d)", "https://h/1")
    s.upsert(p.id, agg, "rejected", "score 40", {"v": assess.ASSESS_VERSION, "total": 40})
    s.commit()
    direct = Job("greenhouse", "ACME", "Product Manager", "https://g/1", direct=True)
    best = dedupe([direct, Job("himalayas", "Acme GmbH", "Product Manager (m/w/d)", "https://h/1")], s.aliases())[0]
    assert best is direct and s.get(p.id, best.all_keys)["assessment"]["total"] == 40


def test_assessments_are_reused_until_the_profile_changes():
    p = Profile.load(EXAMPLE)
    a = {"v": assess.ASSESS_VERSION, "rubric": assess.rubric(p)}
    assert assess.reusable(p, a)
    assert assess.reusable(p, {"v": assess.ASSESS_VERSION})  # stored before fingerprints existed
    p.raw["candidate"] += " Now also open to Porto."
    assert not assess.reusable(p, a)
    assert not assess.reusable(p, {"v": assess.ASSESS_VERSION - 1})


def test_title_index_finds_exactly_the_jobs_that_can_match():
    rng = random.Random(7)
    words = "senior lead head of engineering product marketing manager director data sales designer".split()
    jobs = [
        Job("greenhouse", "Acme", " ".join(rng.sample(words, 3)), f"https://x/{i}", location="Remote, Europe")
        for i in range(400)
    ]
    roles = ["Head of Engineering", "Product Manager", "Data Designer"]
    index = matching.TitleIndex(jobs)
    brute = [j for j in jobs if matching.role_match(j.title, roles)[0] >= matching.ROLE_MIN]
    candidates = index.candidates(roles)
    assert brute and set(map(id, brute)) <= set(map(id, candidates))
    assert len(candidates) < len(jobs)


def test_partner_searches_are_shared_between_subscribers():
    a = {"search_queries": ["Data  Engineer"], "locations": [{"place": "Madrid", "country": "Spain"}]}
    b = {"target_roles": ["data engineer"], "locations": [{"place": " madrid", "country": "spain"}]}
    assert digest.partner_searches(a) == digest.partner_searches(b) == {("data engineer", "spain", "madrid")}


def test_digest_never_sends_a_job_again_as_another_copy():
    t = store.add_pending("copies@example.com", "I want to be a data engineer, remote in Europe.", "daily")
    store.confirm(t["confirm"])
    sid = store.signup_id_by_manage(t["manage"])
    d = {"target_roles": ["Data Engineer"], "work_modes": ["remote"], "remote_regions": ["Europe"]}
    text = "We are hiring a data engineer to build pipelines. " * 20

    def posting(source, company, title, url):
        # direct=True only to skip the live-link check in this test
        return Job(source, company, title, url, location="Remote, Europe", text=text, direct=True)

    first = posting("greenhouse", "Acme", "Data Engineer", "https://g/2")
    store.record_sent(sid, dedupe([first], store.alias_book())[0], Match(90, "", "Data Engineer"))
    # Days later an aggregator copy under another company name turns up – the original is long gone.
    copy = posting("arbeitnow", "ACME GmbH", "Data Engineer (m/w/d)", "https://a/2")
    r = {"id": sid, "derived": d, "frequency": "daily"}
    assert digest.select(r, matching.TitleIndex(dedupe([copy], store.alias_book())), []) == []
    other = posting("arbeitnow", "Globex", "Data Engineer", "https://a/3")
    assert [p.job.company for p in digest.select(r, matching.TitleIndex(dedupe([other], store.alias_book())), [])] == [
        "Globex"
    ]
