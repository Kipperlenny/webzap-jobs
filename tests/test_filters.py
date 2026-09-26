import re
from pathlib import Path

from jobagent import filters
from jobagent.profile import Profile
from jobagent.sources import Job

EXAMPLE = Profile.load(Path(__file__).parent.parent / "profiles" / "example.toml")


def test_example_profile_loads_with_rubric():
    assert EXAMPLE.max_points == 100
    assert EXAMPLE.email


def test_title_and_location_rules():
    assert filters.title_ok(EXAMPLE, "Senior Product Manager")
    assert not filters.title_ok(EXAMPLE, "Junior Product Manager")
    assert filters.location_class(EXAMPLE, "Lisbon, Portugal", "", False) == "local"
    assert filters.location_class(EXAMPLE, "Remote - Europe", "", True) == "remote"
    assert filters.location_class(EXAMPLE, "New York", "", False) == ""


def test_hard_exclusion_patterns():
    j = Job(
        "greenhouse", "A", "Senior Product Manager", "https://x", location="Lisbon", text="Fluent German is required."
    )
    assert filters.prefilter(EXAMPLE, j).startswith("excluded")


def test_written_in_detects_the_language_of_a_description():
    de = "Wir suchen dich für unser Team in Hamburg. Du bist verantwortlich für die Planung und die Umsetzung " * 3
    en = "We are looking for you to join our team. You will be responsible for the planning and delivery of the " * 3
    assert filters.written_in(de) == "german" and filters.written_in(en) == "english"
    assert filters.written_in("Senior engineer, Berlin") == ""


def test_require_rule_needs_a_pattern_or_the_language():
    p = Profile.load(Path(__file__).parent.parent / "profiles" / "example.toml")
    assert not filters.requirement_missing(p, "anything")  # nothing required
    p.require_any = [re.compile(r"\bportuguese\b", re.I)]
    p.require_written_in = ["portuguese"]
    en = "We are looking for you to join our team. You will be responsible for the planning and delivery of the " * 3
    assert filters.requirement_missing(p, en)
    assert not filters.requirement_missing(p, en + " Fluent Portuguese is a plus.")
    pt = "Procuramos uma pessoa para a nossa equipa em Lisboa. Você vai trabalhar com o time de produto e com os " * 3
    assert not filters.requirement_missing(p, pt)
    job = Job("greenhouse", "Acme", "Senior Product Manager", "https://x/3", location="Lisbon", text=en, direct=True)
    assert filters.prefilter(p, job) == "requirement not met"
