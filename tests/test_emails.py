from pathlib import Path

from jobagent import report
from jobagent.profile import Profile
from jobagent.sources import Job

import mailer
from matching import Match

ITEM = {
    "title": "<script>alert(1)</script> Manager",
    "company": "Acme",
    "location": "Remote",
    "url": "https://x/1",
    "why": "matches “Manager”",
    "source": "via Adzuna",
    "sponsored": False,
    "partner": "Adzuna",
    "feedback_url": "https://jobs.example.com/feedback/tok",
}


def test_digest_escapes_job_data_and_labels_partner_links():
    html, text = mailer.render(
        "digest",
        items=[ITEM],
        help=None,
        frequency="daily",
        disclosure=True,
        manage_url="https://m",
        unsubscribe_url="https://u",
    )
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "Partner link · Adzuna" in html and "[Partner link · Adzuna]" in text
    assert "https://u" in html and "https://u" in text


def test_help_box_only_for_vague_profiles():
    vague = {"clarity": 2, "target_roles": ["X"], "questions": ["Where do you live?"], "assumptions": ["We assumed"]}
    box = mailer.help_box(vague, "tok", "de")
    assert box and box["questions"] == ["Where do you live?"] and box["url"].endswith("/de/manage/tok#improve")
    assert mailer.help_box({"clarity": 5, "target_roles": ["X"]}, "tok", "en") is None


def test_precision_report_renders_sections_and_none_message():
    p = Profile.load(Path(__file__).parent.parent / "profiles" / "example.toml")
    j = Job("greenhouse", "Acme", "Senior Product Manager", "https://x/2", direct=True)
    j.score, j.assessment = 80, {"one_line": "Good fit", "why_fits": ["B2B SaaS"], "job_type": "permanent"}
    html, text = report.render(
        p, {"strong": [], "possible": [j], "contract": []}, [], [], {"greenhouse:a": 3}, 10, 1, ""
    )
    assert "Senior Product Manager" in html and "B2B SaaS" in html
    assert p.out("none_message") in html and p.out("none_message") in text


def test_first_email_intro_is_shown_and_escaped():
    html, text = mailer.render(
        "digest",
        items=[],
        help=None,
        intro="We read your <wish> carefully.",
        frequency="daily",
        disclosure=False,
        manage_url="https://m",
        unsubscribe_url="https://u",
    )
    assert "We read your &lt;wish&gt; carefully." in html
    assert text.startswith("We read your <wish> carefully.")


def test_digest_item_reason_and_source_in_the_persons_language():
    job = Job("jooble", "Acme", "PM", "https://x", partner="Jooble")
    m = Match(80, "", "Product Manager", "remote", "worldwide", ("saas", "b2b"))
    en = mailer.digest_item(job, m, "en", "https://f")
    assert en["why"] == "matches “Product Manager” · remote (worldwide) · mentions saas, b2b"
    assert en["source"] == "via Jooble"
    assert mailer.digest_item(job, m, "en", "https://f", sponsor_id="acme-q4")["source"] == "sponsored listing"
    local = mailer.digest_item(
        Job("greenhouse", "A", "PM", "u", direct=True), m._replace(loc_kind="local", where="Lyon"), "en", "f"
    )
    assert "· Lyon ·" in local["why"] and local["source"] == "employer's career page"


def test_welcome_email_has_manage_link_and_says_no_mass_mailings():
    html, text = mailer.render("welcome", "en", manage_url="https://jobs.example.com/en/manage/tok")
    assert "https://jobs.example.com/en/manage/tok" in html and "https://jobs.example.com/en/manage/tok" in text
    assert "no mass mailings" in text


def test_digest_subject_counts():
    assert mailer.digest_subject("en", 0) == "Help us find your next job"
    assert mailer.digest_subject("en", 1) == "1 new job match for you"
    assert mailer.digest_subject("en", 3) == "3 new job matches for you"
