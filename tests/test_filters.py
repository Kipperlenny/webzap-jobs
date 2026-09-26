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
