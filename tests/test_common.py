from jobagent.common import is_public_http_url, source_label, to_eur
from jobagent.sources import Job


def test_url_guard_rejects_non_http_and_internal_hosts():
    assert not is_public_http_url("javascript:alert(1)")
    assert not is_public_http_url("data:text/html,x")
    assert not is_public_http_url("http://127.0.0.1:8010/manage/x")
    assert not is_public_http_url("http://localhost/")
    assert not is_public_http_url("http://10.0.0.5/")
    assert not is_public_http_url("http://[::1]/")


def test_to_eur():
    assert to_eur(100, "EUR") == 100
    assert to_eur("100", "usd") == 86
    assert to_eur(None, "EUR") is None
    assert to_eur("n/a", "EUR") is None


def test_source_label_names_the_source():
    assert source_label(Job("remoteok", "A", "T", "https://x")) == "source: Remote OK"
    assert source_label(Job("greenhouse", "A", "T", "https://x", direct=True)) == "employer's career page"
    assert source_label(Job("adzuna", "A", "T", "https://x", partner="Adzuna")) == "via Adzuna"
