"""External links: only sites that fit the person, with their search filled in, rarely, and only in emails we send
anyway."""

from pathlib import Path

import pytest

from jobagent import report, run
from jobagent.profile import Profile
from jobagent.store import Store

import digest
import external
import mailer
import store

HAMBURG = {
    "target_roles": ["Engineering Manager"],
    "search_queries": ["Engineering Manager"],
    "locations": [{"place": "Hamburg", "country": "Germany"}],
    "languages": ["Deutsch", "English"],
    "work_modes": ["hybrid"],
}


def names(xs):
    return [x["name"] for x in xs]


def test_country_boards_get_the_persons_search_and_place():
    out = external.suggest(HAMBURG, "de")
    assert names(out)[:2] == ["StepStone", "XING Jobs"]
    assert out[0]["url"] == "https://www.stepstone.de/jobs/engineering-manager/in-hamburg"
    assert out[1]["url"] == "https://www.xing.com/jobs/search?keywords=Engineering+Manager&location=Hamburg"
    assert "karriere.at" not in names(out) and "jobs.ac.uk" not in names(out)


def test_field_specific_sites_come_first_and_need_the_field():
    scientist = dict(HAMBURG, target_roles=["Research Scientist"], fields=["research"])
    assert names(external.suggest(scientist, "de"))[0] == "academics.de"
    assert "academics.de" not in names(external.suggest(HAMBURG, "de"))


def test_language_type_and_remote_rules():
    no_german = dict(HAMBURG, languages=["English"])
    assert "StepStone" not in names(external.suggest(no_german, "en"))
    assert "StepStone" in names(external.suggest(dict(no_german, languages=[]), "de"))  # email language counts
    it = dict(HAMBURG, fields=["software"], employment_types=["freelance"])
    assert "GULP (Randstad)" in names(external.suggest(it, "de"))
    assert "GULP (Randstad)" not in names(external.suggest(dict(it, employment_types=["permanent"]), "de"))
    remote = dict(HAMBURG, work_modes=["remote"])
    assert external.suggest(remote, "de")[0]["url"] == "https://www.stepstone.de/jobs/engineering-manager"


def test_queries_are_encoded_and_nothing_without_a_search():
    odd = dict(HAMBURG, target_roles=['C++ & "Rust" <dev>'])
    url = external.suggest(odd, "de")[1]["url"]
    assert "keywords=C%2B%2B+%26+%22Rust%22+%3Cdev%3E" in url
    assert external.suggest({"locations": HAMBURG["locations"]}, "de") == []
    only_queries = dict(HAMBURG, target_roles=[], search_queries=["Data Engineer"])
    assert external.suggest(only_queries, "de")[0]["query"] == "Data Engineer"


def test_catalog_urls_are_https():
    for site in external.CATALOG["site"]:
        assert site["search"].startswith("https://") and site.get("search_where", "https://").startswith("https://")


def test_each_link_at_most_once_per_repeat_period():
    t = store.add_pending("ext@example.com", "Engineering manager in Hamburg.", "daily")
    store.confirm(t["confirm"])
    sid = store.signup_id_by_manage(t["manage"])
    r = {"id": sid, "derived": HAMBURG, "lang": "de"}
    first = digest.externals_for(r)
    assert names(first) == ["StepStone", "XING Jobs"]
    store.mark_externals_shown(sid, [x["url"] for x in first])
    assert "StepStone" not in names(digest.externals_for(r))
    assert store.externals_due(sid, [first[0]["url"]], repeat_days=0) == [first[0]["url"]]


def test_digest_email_labels_external_links_clearly():
    ext = [{"name": "StepStone", "url": "https://s/<x>", "query": "<b>PM</b>", "where": "Hamburg"}]
    html, text = mailer.render(
        "digest",
        "de",
        items=[],
        help=None,
        frequency="daily",
        disclosure=False,
        externals=ext,
        manage_url="https://m",
        unsubscribe_url="https://u",
    )
    assert "Externer Link" in html and "&lt;b&gt;PM&lt;/b&gt;" in html and "https://s/&lt;x&gt;" in html
    assert "[Externer Link] Suche nach „<b>PM</b>“ in Hamburg auf StepStone" in text
    assert "keine Partnerschaft" in text
    plain, _ = mailer.render(
        "digest", "de", items=[], help=None, frequency="daily", disclosure=False, manage_url="m", unsubscribe_url="u"
    )
    assert "Externer Link" not in plain


def test_agent_profile_links_come_back_only_after_the_repeat_period(tmp_path):
    p = Profile.load(Path(__file__).parent.parent / "profiles" / "example.toml")
    p.raw["external"] = [{"name": "Company portal", "url": "https://co.example/jobs", "note": "no job feed"}]
    s = Store(tmp_path / "agent.sqlite3")
    due = run.due_externals(p, s)
    assert due == [{"name": "Company portal", "url": "https://co.example/jobs", "note": "no job feed"}]
    s.mark_externals_shown(p.id, [due[0]["url"]])
    assert run.due_externals(p, s) == []
    html, text = report.render(p, {"strong": [], "possible": [], "contract": []}, [], [], {}, 0, 0, "", due)
    assert "External link" in html and "https://co.example/jobs" in text
    p.raw["external"] = [{"name": "Bad", "url": "javascript:alert(1)"}]
    with pytest.raises(ValueError):
        run.due_externals(p, s)
