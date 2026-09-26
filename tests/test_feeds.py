"""Partner feed request details that the providers are strict about (found in production)."""

from jobagent import feeds


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def test_jooble_uses_main_domain_and_country_in_location(monkeypatch):
    calls = []
    monkeypatch.setenv("JOOBLE_API_KEY", "k")
    monkeypatch.setattr(
        feeds.requests,
        "post",
        lambda url, **kw: calls.append((url, kw))
        or FakeResponse(
            {
                "jobs": [
                    {"title": "Nurse", "company": "Clinic", "link": "https://jooble.org/job/1", "location": "Valencia"}
                ]
            }
        ),
    )
    jobs = feeds.jooble("nurse", "spain", "Valencia")
    assert calls[0][0] == "https://jooble.org/api/k"
    assert calls[0][1]["json"]["location"] == "Valencia, Spain"
    assert jobs[0].partner == "Jooble"


def test_careerjet_sends_referer(monkeypatch):
    calls = []
    monkeypatch.setenv("CAREERJET_API_KEY", "k")
    monkeypatch.setenv("CAREERJET_USER_IP", "203.0.113.5")
    monkeypatch.setenv("BASE_URL", "https://jobs.example.com")
    monkeypatch.setattr(feeds.requests, "get", lambda url, **kw: calls.append(kw) or FakeResponse({"jobs": []}))
    feeds.careerjet("nurse", "spain")
    assert calls[0]["headers"]["Referer"] == "https://jobs.example.com/"
    assert calls[0]["params"]["locale_code"] == "es_ES"


def test_unknown_country_is_skipped():
    assert feeds.adzuna("x", "atlantis") == [] and feeds.careerjet("x", "atlantis") == []
