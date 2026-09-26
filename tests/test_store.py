import sqlite3

from jobagent.sources import Job

import store
from matching import Match

MATCH = Match(80, "matches “PM”", "PM")


def new_entry(email="person@example.com", text="I want to be a senior product manager in Lisbon."):
    t = store.add_pending(email, text, "weekly")
    assert store.confirm(t["confirm"])
    return t


def test_tokens_and_email_are_never_stored_in_clear():
    t = new_entry("clear@example.com")
    raw = store.DB_PATH.read_bytes()
    assert b"clear@example.com" not in raw
    assert all(t[k].encode() not in raw for k in ("confirm", "manage", "delete"))


def test_confirm_queues_extraction_and_edit_requeues():
    t = new_entry()
    assert store.get_by_manage(t["manage"])["derived_status"] == "pending"
    sid = store.signup_id_by_manage(t["manage"])
    while (task := store.claim_task())["signup_id"] != sid:  # skip tasks queued by other tests
        store.finish_task(task, {"summary": "x"}, "test")
    store.finish_task(task, {"summary": "x", "target_roles": ["PM"]}, "test:model:v2")
    assert store.get_by_manage(t["manage"])["derived_status"] == "done"
    store.update(t["manage"], "A different text about product management.", "daily")
    assert store.get_by_manage(t["manage"])["derived_status"] == "pending"


def test_feedback_unsubscribe_and_cascading_delete():
    t = new_entry("fb@example.com")
    sid = store.signup_id_by_manage(t["manage"])
    tok = store.record_sent(sid, Job("greenhouse", "Acme", "PM", "https://x/1"), MATCH)
    assert store.feedback_set(tok, "down", "not this company")
    assert store.feedback_signals(sid)["excluded_companies"] == {"acme"}
    assert store.unsubscribe(t["manage"])
    assert store.get_by_manage(t["manage"])["frequency"] == "paused"
    assert store.delete(t["delete"])
    with sqlite3.connect(store.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM sent_jobs WHERE signup_id=?", (sid,)).fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM tasks WHERE signup_id=?", (sid,)).fetchone()[0] == 0


def test_resignup_replaces_old_entry():
    old = new_entry("again@example.com", "First text about product management.")
    new_entry("again@example.com", "Second text about product management.")
    assert store.get_by_manage(old["manage"]) is None


def test_feedback_signals_and_reanalysis_after_five_votes():
    t = new_entry("learn@example.com")
    sid = store.signup_id_by_manage(t["manage"])
    while (task := store.claim_task())["signup_id"] != sid:  # finish the initial extraction
        store.finish_task(task, {"summary": "x"}, "test")
    store.finish_task(task, {"summary": "x", "target_roles": ["PM"]}, "test:m:v3:fb0")
    votes = [
        ("up", ""),
        ("down", "not my field"),
        ("down", "not my field; wrong location"),
        ("down", "too junior"),
        ("down", "salary too low"),
    ]
    for i, (vote, reason) in enumerate(votes):
        job = Job("greenhouse", f"Co{i}", "Product Manager", f"https://x/l{i}", location="Porto, Portugal")
        store.feedback_set(store.record_sent(sid, job, MATCH), vote, reason)
    sig = store.feedback_signals(sid)
    assert sig["role_up"] == {"PM": 1} and sig["role_down"] == {"PM": 2}
    assert sig["too_junior"] and sig["salary_strict"] and sig["bad_places"] == {"porto"}
    task = store.claim_task()  # 5 new votes → re-analysis queued, with the feedback attached
    assert task["signup_id"] == sid and len(task["feedback"]) == 5
