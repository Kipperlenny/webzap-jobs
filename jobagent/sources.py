"""Job sources. Each fetcher returns a list of Job; failures are logged and skipped."""

import concurrent.futures as cf
import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime

import requests

from .common import USER_AGENT

log = logging.getLogger(__name__)
UA = {"User-Agent": USER_AGENT}
TIMEOUT = 30
# ISO codes some ATS return instead of names, so search.toml can use plain country names.
COUNTRIES = {
    "AT": "Austria",
    "BE": "Belgium",
    "CH": "Switzerland",
    "CZ": "Czechia",
    "DE": "Germany",
    "DK": "Denmark",
    "ES": "Spain",
    "FI": "Finland",
    "FR": "France",
    "GB": "United Kingdom",
    "GR": "Greece",
    "IE": "Ireland",
    "IT": "Italy",
    "NL": "Netherlands",
    "NO": "Norway",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "SE": "Sweden",
    "US": "United States",
    "CA": "Canada",
}


@dataclass
class Job:
    source: str
    company: str
    title: str
    url: str
    location: str = ""
    remote: bool = False
    text: str = ""
    posted: datetime | None = None
    employment_type: str = ""
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str | None = None
    direct: bool = False  # True = employer's own ATS, False = aggregator
    partner: str = ""  # set for paid partner links (Adzuna, Jooble, Careerjet): must be labelled
    # filled in per profile by the pipeline
    loc_class: str = ""
    contract: bool = False
    assessment: dict = field(default_factory=dict)
    score: int = 0
    status: str = ""
    # set by dedup.dedupe(): the stable id of the opening, the keys of its other copies, how many copies there were
    uid: str = ""
    aliases: set[str] = field(default_factory=set)
    copies: int = 1

    @property
    def raw_key(self) -> str:
        """This copy's key: normalised company + title, ignoring gender tags and per-country suffixes."""
        title = re.sub(r"\s*\((m|f|w|d|x|h)(/(m|f|w|d|x|h))+\)", "", self.title, flags=re.I)
        title = title.split(" | ")[0]  # "Engineering Manager | Germany | Remote" -> one role across countries
        return f"{_norm(self.company)}|{_norm(title)}"

    @property
    def key(self) -> str:
        """The opening's id: stable across copies and runs once dedup.dedupe() has run, else this copy's key."""
        return self.uid or self.raw_key

    @property
    def all_keys(self) -> tuple[str, ...]:
        """The id first, then the keys of its other copies – for looking up what we already know about the job."""
        return (self.key, *sorted(self.aliases - {self.key}))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def strip_html(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t\xa0]+", " ", html.unescape(s)).strip()


def parse_dt(v) -> datetime | None:
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            v = float(v)
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=UTC)
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, OSError):
        return None


def get_json(url, **kw):
    r = requests.get(url, headers=UA, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r.json()


# ---------- employer ATS boards (direct postings) ----------


def greenhouse(board):
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", params={"content": "true"})
    out = []
    for j in d.get("jobs", []):
        out.append(
            Job(
                "greenhouse",
                j.get("company_name") or board,
                j["title"],
                j["absolute_url"],
                location=(j.get("location") or {}).get("name", ""),
                text=strip_html(j.get("content", "")),
                posted=parse_dt(j.get("first_published") or j.get("updated_at")),
                direct=True,
            )
        )
    return out


def lever(company):
    d = get_json(f"https://api.lever.co/v0/postings/{company}", params={"mode": "json"})
    out = []
    for j in d:
        c = j.get("categories") or {}
        locs = c.get("allLocations") or [c.get("location", "")]
        loc = "; ".join(filter(None, locs))
        if j.get("country") in COUNTRIES:
            loc += f"; {COUNTRIES[j['country']]}"
        text = "\n".join(
            filter(
                None,
                [
                    j.get("descriptionPlain"),
                    *[f"{li.get('text')}\n{strip_html(li.get('content', ''))}" for li in j.get("lists") or []],
                    j.get("additionalPlain"),
                ],
            )
        )
        sr = j.get("salaryRange") or {}
        out.append(
            Job(
                "lever",
                company,
                j["text"],
                j["hostedUrl"],
                location=loc,
                remote=j.get("workplaceType") == "remote",
                text=text,
                posted=parse_dt(j.get("createdAt")),
                employment_type=c.get("commitment", ""),
                salary_min=sr.get("min"),
                salary_max=sr.get("max"),
                currency=sr.get("currency"),
                direct=True,
            )
        )
    return out


def ashby(org):
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{org}", params={"includeCompensation": "true"})
    out = []
    for j in d.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        addr = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry", "")
        comp = (j.get("compensation") or {}).get("summaryComponents") or []
        sal = next((c for c in comp if c.get("compensationType") == "Salary"), {})
        out.append(
            Job(
                "ashby",
                org,
                j["title"],
                j["jobUrl"],
                location="; ".join(filter(None, locs + [addr])),
                remote=bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
                text=j.get("descriptionPlain", ""),
                posted=parse_dt(j.get("publishedAt")),
                employment_type=j.get("employmentType", ""),
                salary_min=sal.get("minValue"),
                salary_max=sal.get("maxValue"),
                currency=sal.get("currencyCode"),
                direct=True,
            )
        )
    return out


def smartrecruiters(company):
    out, offset = [], 0
    while offset < 1000:
        d = get_json(
            f"https://api.smartrecruiters.com/v1/companies/{company}/postings", params={"limit": 100, "offset": offset}
        )
        for j in d.get("content", []):
            loc = j.get("location") or {}
            place = ", ".join(
                filter(
                    None,
                    [
                        loc.get("city"),
                        loc.get("region"),
                        COUNTRIES.get((loc.get("country") or "").upper(), loc.get("country")),
                    ],
                )
            )
            out.append(
                Job(
                    "smartrecruiters",
                    (j.get("company") or {}).get("name") or company,
                    j["name"],
                    f"https://jobs.smartrecruiters.com/{company}/{j['id']}",
                    location=place,
                    remote=bool(loc.get("remote")),
                    posted=parse_dt(j.get("releasedDate")),
                    employment_type=(j.get("typeOfEmployment") or {}).get("label", ""),
                    direct=True,
                )
            )
        offset += 100
        if offset >= d.get("totalFound", 0):
            break
    # The list has no descriptions; fetch them only for the few jobs that survive the title filter (see enrich()).
    return out


def personio(company):
    r = requests.get(f"https://{company}.jobs.personio.de/xml", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    # Safe with the stdlib parser: no external entities, and expat >= 2.4.1 blocks entity-expansion ("billion laughs").
    for pos in ET.fromstring(r.content).iter("position"):  # noqa: S314

        def g(tag: str, pos=pos) -> str:
            return (pos.findtext(tag) or "").strip()

        text = "\n".join(strip_html(d.findtext("value") or "") for d in pos.iter("jobDescription"))
        offices = ", ".join(filter(None, [g("office")] + [o.text for o in pos.iter("additionalOffice") if o.text]))
        out.append(
            Job(
                "personio",
                g("subcompany") or company,
                g("name"),
                f"https://{company}.jobs.personio.de/job/{g('id')}",
                location=offices,
                remote="remote" in (offices + g("name")).lower(),
                text=text,
                posted=parse_dt(g("createdAt")),
                employment_type=f"{g('employmentType')} {g('schedule')}".strip(),
                direct=True,
            )
        )
    return out


def recruitee(company):
    d = get_json(f"https://{company}.recruitee.com/api/offers/")
    out = []
    for j in d.get("offers", []):
        loc = ", ".join(filter(None, [j.get("city"), j.get("country")]))
        sal = j.get("salary") or {}
        out.append(
            Job(
                "recruitee",
                j.get("company_name") or company,
                j["title"],
                j.get("careers_url") or j.get("url"),
                location=loc,
                remote=bool(j.get("remote")),
                text=strip_html(f"{j.get('description', '')}\n{j.get('requirements', '')}"),
                posted=parse_dt(j.get("published_at") or j.get("created_at")),
                employment_type=j.get("employment_type_code", ""),
                salary_min=sal.get("min"),
                salary_max=sal.get("max"),
                currency=sal.get("currency"),
                direct=True,
            )
        )
    return out


def enrich(job):
    """Fill in the description for sources whose list endpoint has none (SmartRecruiters)."""
    if job.text or job.source != "smartrecruiters":
        return job
    company, jid = job.url.rstrip("/").split("/")[-2:]
    try:
        d = get_json(f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{jid}")
        secs = (d.get("jobAd") or {}).get("sections") or {}
        job.text = "\n".join(
            strip_html((secs.get(k) or {}).get("text", ""))
            for k in ("companyDescription", "jobDescription", "qualifications", "additionalInformation")
        )
        job.url = d.get("postingUrl") or job.url
    except Exception as e:  # noqa: BLE001
        log.info("could not enrich %s: %s", job.url, e)
    return job


# ---------- aggregators ----------


def remotive():
    out = []
    for cat in ("software-dev", "project-management", "all-others"):
        for j in get_json("https://remotive.com/api/remote-jobs", params={"category": cat}).get("jobs", []):
            out.append(
                Job(
                    "remotive",
                    j["company_name"],
                    j["title"],
                    j["url"],
                    location=j.get("candidate_required_location", ""),
                    remote=True,
                    text=strip_html(j.get("description", "")),
                    posted=parse_dt(j.get("publication_date")),
                    employment_type=j.get("job_type", ""),
                )
            )
    return out


def remoteok():
    out = []
    for j in get_json("https://remoteok.com/api")[1:]:
        if not j.get("position"):
            continue
        out.append(
            Job(
                "remoteok",
                j.get("company", ""),
                j["position"],
                j.get("url", ""),
                location=j.get("location", ""),
                remote=True,
                text=strip_html(j.get("description", "")),
                posted=parse_dt(j.get("epoch")),
                salary_min=j.get("salary_min") or None,
                salary_max=j.get("salary_max") or None,
                currency="USD",
            )
        )
    return out


def arbeitnow():
    out, url = [], "https://www.arbeitnow.com/api/job-board-api"
    for _ in range(6):
        d = get_json(url)
        for j in d.get("data", []):
            out.append(
                Job(
                    "arbeitnow",
                    j["company_name"],
                    j["title"],
                    j["url"],
                    location=j.get("location", ""),
                    remote=bool(j.get("remote")),
                    text=strip_html(j.get("description", "")),
                    posted=parse_dt(j.get("created_at")),
                    employment_type=" ".join(j.get("job_types") or []),
                )
            )
        url = (d.get("links") or {}).get("next")
        if not url:
            break
    return out


def himalayas(queries):
    out = []
    for q in queries or [""]:
        d = get_json("https://himalayas.app/jobs/api/search", params={"q": q, "limit": 50})
        for j in d.get("jobs", []):
            out.append(
                Job(
                    "himalayas",
                    j.get("companyName", ""),
                    j["title"],
                    j.get("applicationLink") or j.get("guid"),
                    location="; ".join(j.get("locationRestrictions") or []) or "Anywhere",
                    remote=True,
                    text=strip_html(j.get("description", "")),
                    posted=parse_dt(j.get("pubDate")),
                    employment_type=j.get("employmentType", ""),
                    salary_min=j.get("minSalary"),
                    salary_max=j.get("maxSalary"),
                    currency=j.get("currency"),
                )
            )
    return out


def jobicy(geos):
    out = []
    for geo in geos or ["anywhere"]:
        try:
            d = get_json("https://jobicy.com/api/v2/remote-jobs", params={"count": 100, "geo": geo})
        except requests.HTTPError:
            continue
        for j in d.get("jobs", []):
            out.append(
                Job(
                    "jobicy",
                    j.get("companyName", ""),
                    html.unescape(j["jobTitle"]),
                    j["url"],
                    location=j.get("jobGeo", ""),
                    remote=True,
                    text=strip_html(j.get("jobDescription", "")),
                    posted=parse_dt(j.get("pubDate")),
                    employment_type=" ".join(j.get("jobType") or []),
                    salary_min=j.get("salaryMin"),
                    salary_max=j.get("salaryMax"),
                    currency=j.get("salaryCurrency"),
                )
            )
    return out


AGGREGATORS = {
    "remotive": remotive,
    "remoteok": remoteok,
    "arbeitnow": arbeitnow,
    "himalayas": himalayas,
    "jobicy": jobicy,
}
BOARDS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "personio": personio,
    "recruitee": recruitee,
}


def fetch(spec: dict, queries: list[str] = (), jobicy_geo: list[str] = ()) -> tuple[list[Job], dict]:
    """Fetch every board/aggregator named in `spec` ({"greenhouse": [...], "aggregators": [...], …}) once, in parallel.
    Returns (jobs with a usable http(s) link, per-source counts or errors)."""
    tasks = {f"{kind}:{b}": (fn, (b,)) for kind, fn in BOARDS.items() for b in spec.get(kind, [])}
    for a in spec.get("aggregators", []):
        args = (list(queries),) if a == "himalayas" else (list(jobicy_geo),) if a == "jobicy" else ()
        tasks[a] = (AGGREGATORS[a], args)
    jobs, stats = [], {}
    with cf.ThreadPoolExecutor(12) as ex:
        futs = {ex.submit(fn, *args): name for name, (fn, args) in tasks.items()}
        for fut in cf.as_completed(futs):
            name = futs[fut]
            try:
                res = [j for j in fut.result() if j.url.startswith(("https://", "http://")) and j.company.strip()]
                jobs += res
                stats[name] = len(res)
            except Exception as e:  # noqa: BLE001 – one broken source must not kill the run
                log.warning("source %s failed: %s", name, e.__class__.__name__)
                stats[name] = f"error: {e.__class__.__name__}"
    return jobs, stats


def merge_specs(specs: list[dict]) -> dict:
    """Union of several source specs (e.g. from all precision profiles)."""
    out: dict[str, list] = {}
    for spec in specs:
        for k, v in spec.items():
            if isinstance(v, list):
                out[k] = sorted(set(out.get(k, [])) | set(v))
    return out
