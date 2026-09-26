from jobagent.sources import Job

import matching

PROFILE = {
    "target_roles": ["Head of Engineering", "Senior Engineering Manager", "Engineering Manager"],
    "seniority": ["senior", "director"],
    "locations": [{"place": "Valencia", "country": "Spain"}],
    "remote_regions": ["Europe", "Spain"],
    "work_modes": ["remote", "hybrid"],
    "keywords": ["platform", "hiring", "cloud"],
    "deal_breakers": ["agency jobs"],
    "salary": {"minimum": 80000, "currency": "EUR", "period": "year"},
}


def job(title, location="Remote, Europe", text="We build a cloud platform and are hiring.", **kw):
    return Job(
        "greenhouse",
        kw.pop("company", "Acme"),
        title,
        "https://jobs.example.com/1",
        location=location,
        remote=kw.pop("remote", True),
        text=text,
        direct=True,
        **kw,
    )


def test_role_match_needs_the_key_word():
    roles = PROFILE["target_roles"]
    assert matching.role_match("Head of Engineering", roles)[0] == 1.0
    assert matching.role_match("Head of Marketing", roles)[0] < matching.ROLE_MIN
    assert matching.role_match("Senior Software Engineer", roles)[0] < matching.ROLE_MIN


def test_good_job_scores_high_with_reason():
    m = matching.score(job("Senior Engineering Manager"), PROFILE)
    assert m.score >= 70
    assert m.role == "Senior Engineering Manager" and "Senior Engineering Manager" in m.why


def test_hard_rules_exclude():
    assert matching.score(job("Junior Engineering Manager"), PROFILE) is None  # seniority
    assert matching.score(job("Engineering Manager", location="Austin, TX", remote=False), PROFILE) is None
    assert matching.score(job("Engineering Manager", text="Great agency jobs for you, cloud"), PROFILE) is None
    assert matching.score(job("Engineering Manager", salary_max=50000, currency="EUR"), PROFILE) is None
    assert matching.score(job("Engineering Manager"), PROFILE, matching.Signals(excluded_companies={"acme"})) is None


def test_title_alone_is_not_enough_when_nothing_relevant_is_mentioned():
    assert matching.score(job("Engineering Manager", text="Sell insurance door to door. " * 30), PROFILE) is None


def test_local_job_in_home_place_matches():
    res = matching.score(job("Engineering Manager", location="Valencia, Spain", remote=False), PROFILE)
    assert res and "Valencia" in res.why


def test_talent_pools_are_not_jobs():
    assert matching.score(job("Engineering Manager – Talent Pool"), PROFILE) is None
    assert (
        matching.score(job("Engineering Manager", text="Join our talent pool for future opportunities. cloud"), PROFILE)
        is None
    )


def test_generic_words_do_not_prove_relevance():
    profile = {**PROFILE, "keywords": ["Europe", "remote", "hiring", "Valencia", "kubernetes"]}
    assert matching.score(job("Engineering Manager", text="Remote in Europe, we are hiring! " * 30), profile) is None
    assert matching.score(job("Engineering Manager", text="Remote in Europe, Kubernetes platform. " * 30), profile)


def test_feedback_signals_adjust_matching():
    base = matching.score(job("Engineering Manager"), PROFILE).score
    # 👍 on a role and on similar titles ranks similar jobs higher
    liked = matching.Signals(role_up={"Engineering Manager": 2}, liked_words={"engineering", "platform"})
    assert matching.score(job("Engineering Manager, Platform"), PROFILE, liked).score > base
    # two "not my field" votes drop the role (the next best role must match on its own)
    blocked = matching.Signals(role_down={"Engineering Manager": 2, "Senior Engineering Manager": 2})
    assert matching.score(job("Engineering Manager"), PROFILE, blocked) is None
    # level and location feedback
    assert matching.score(job("Associate Engineering Manager"), PROFILE, matching.Signals(too_junior=True)) is None
    assert matching.score(job("Head of Engineering"), PROFILE, matching.Signals(too_senior=True)) is None
    far = job("Engineering Manager", location="Valencia, Spain", remote=False)
    assert matching.score(far, PROFILE, matching.Signals(bad_places={"valencia"})) is None


def test_first_place():
    assert matching.first_place("Barcelona, Catalonia, Spain") == "barcelona"
    assert matching.first_place("") == ""


def test_short_partner_snippets_are_judged_on_role_and_location_only():
    snippet = job("Engineering Manager", text="Lead our EU team, fully remote.", partner="Jooble")
    snippet.direct = False
    assert matching.score(snippet, PROFILE)  # too short to demand keywords
    long_text = job("Engineering Manager", text="Sell insurance door to door. " * 30)
    assert matching.score(long_text, PROFILE) is None  # full posting without any of their topics


def test_bare_remote_needs_a_named_region_unless_worldwide():
    us_remote = job("Engineering Manager", location="Remote", text="Join our platform team, cloud, hiring.")
    assert matching.score(us_remote, PROFILE) is None  # profile wants Europe/Spain – "Remote" alone proves nothing
    eu_remote = job("Engineering Manager", location="Remote", text="Remote across Europe. Cloud platform.")
    assert matching.score(eu_remote, PROFILE)
    anywhere = {**PROFILE, "remote_regions": ["worldwide"], "locations": []}
    assert matching.score(us_remote, anywhere)
