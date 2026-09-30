"""Company discovery: from "companies like X, Y, Z" ([companies] like) and the person's 👍, find similar employers and
the job boards they post on, so the search is not limited to hand-made source lists.

Per run, with small budgets:
1. rate – companies seen on aggregators (without a board in our sources) that hire where the person can work: the model
   says which are like the wanted ones. Each company is judged once per profile.
2. suggest – the model names more similar companies, with their websites (one call per profile).
3. resolve (no model) – for every wanted company not looked up yet, find its job board: board links on its website or
   career page, else likely board names checked against the ATS APIs. Found boards are fetched in the same run and
   from then on in every run, for all profiles. A company without a board we can read is tried again after 30 days.
"""

import concurrent.futures as cf
import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlparse

import requests

from . import connectors, filters
from .common import USER_AGENT, is_public_http_url
from .dedup import _CORP_EXTRA, _LEGAL, company_key, company_match, company_tokens

log = logging.getLogger("jobagent")

UA = {"User-Agent": USER_AGENT}
BATCH = 20
RETRY_DAYS = 30
# Board links on career pages → (ats, slug). Workday slugs are "tenant/wdN/Site" (see sources.workday).
BOARD_LINKS = [
    (
        "greenhouse",
        re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([a-z0-9_-]+)", re.I),
    ),
    ("lever", re.compile(r"jobs\.lever\.co/([a-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_.%-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([a-z0-9_-]+)", re.I)),
    ("personio", re.compile(r"([a-z0-9-]+)\.jobs\.personio\.(?:de|com)", re.I)),
    ("recruitee", re.compile(r"([a-z0-9-]+)\.recruitee\.com", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([a-z0-9_-]+)", re.I)),
    ("workday", re.compile(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[a-z]{2}/)?([a-z0-9_-]+)", re.I)),
]
MAX_PAGES = 8
NOT_SLUGS = {"embed", "j", "api", "jobs", "job", "v1", "apply", "static", "assets", "wday", "www", "careers"}
CAREER_LINK = re.compile(
    r"""href=["']([^"'#]*(?:career|karriere|jobs|job-|stellen|empleo|trabaja|join-us|joinus|vacanc)[^"'#]*)["']""", re.I
)


# ---------- job boards ----------


def board_links(html: str) -> list[tuple[str, str]]:
    """Every (ats, slug) linked from a page, in order of appearance."""
    out = []
    for ats, rx in BOARD_LINKS:
        for m in rx.finditer(html or ""):
            slug = "/".join(m.groups()) if ats == "workday" else m.group(1)
            if slug.split("/")[-1].lower() not in NOT_SLUGS and (ats, slug) not in out:
                out.append((ats, slug))
    return out


def _get(url: str, **kw):
    return requests.get(url, headers=UA, timeout=15, **kw)


def probe(ats: str, slug: str) -> tuple[bool, str, int]:
    """Does this board exist? Returns (ok, the board's company name where the API tells it, number of open jobs)."""
    try:
        if ats == "greenhouse":
            r = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
            if not r.ok:
                return False, "", 0
            name = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}").json().get("name", "")
            return True, name, len(r.json().get("jobs", []))
        if ats == "lever":
            r = _get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json", "limit": 1})
            return r.ok, "", len(r.json()) if r.ok else 0
        if ats == "ashby":
            r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
            return r.ok, "", len(r.json().get("jobs", [])) if r.ok else 0
        if ats == "smartrecruiters":  # answers 200 with nothing for any name – only a board with jobs counts
            r = _get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", params={"limit": 1})
            d = r.json() if r.ok else {}
            content = d.get("content", [])
            name = (content[0].get("company") or {}).get("name", "") if content else ""
            return bool(content), name, d.get("totalFound", 0)
        if ats == "personio":
            r = _get(f"https://{slug}.jobs.personio.de/xml", allow_redirects=False)
            ok = r.status_code == 200 and b"<workzag-jobs" in r.content[:500]
            return ok, "", r.content.count(b"<position>") if ok else 0
        if ats == "recruitee":
            r = _get(f"https://{slug}.recruitee.com/api/offers/")
            return r.ok, "", len(r.json().get("offers", [])) if r.ok else 0
        if ats == "workable":
            r = _get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
            d = r.json() if r.ok else {}
            return r.ok, d.get("name", ""), len(d.get("jobs", []))
        if ats == "successfactors":
            r = _get(f"https://{slug}/services/rss/job/", params={"locale": "en_US"})
            return r.ok and b"<rss" in r.content[:300], "", r.content.count(b"<item>")
        if ats == "workday":
            tenant, wd, site = slug.split("/")
            r = requests.post(
                f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
                json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                headers={**UA, "Accept": "application/json"},
                timeout=15,
            )
            d = r.json() if r.ok else {}
            return "jobPostings" in d, "", d.get("total", 0)
    except (requests.RequestException, ValueError, AttributeError):
        pass
    return False, "", 0


def guesses(name: str) -> list[str]:
    """Likely board names: 'Plain Concepts S.L.' → plainconcepts, plain-concepts."""
    words = [re.sub(r"[^a-z0-9]", "", w) for w in company_tokens(name)]
    words = [w for w in words if len(w) > 1]  # "S.L." → s, l
    # Hyphens kept, legal forms dropped: "T-Systems Iberia" → t-systemsiberia
    raw = [w for w in (name or "").lower().split() if re.sub(r"[^a-z0-9]", "", w) not in _LEGAL]
    kept = "".join(re.sub(r"[^a-z0-9-]", "", w) for w in raw)
    out = ["".join(words), "-".join(words), company_key(name), kept]
    generic = _CORP_EXTRA | _LEGAL  # "systems" from "T-Systems": some other company's board
    return [g for i, g in enumerate(out) if len(g) >= 3 and g not in generic and g not in out[:i]]


def _page(url: str) -> str:
    if not is_public_http_url(url):  # websites come from a model or a posting – never fetch internal addresses
        return ""
    try:
        r = _get(url)
        return r.text[:2_000_000] if r.ok and "html" in r.headers.get("content-type", "") else ""
    except requests.RequestException:
        return ""


def resolve(name: str, website: str = "") -> tuple[str, str] | None:
    """Find the job board of a company: links on its website and career pages first, then guessed names."""
    if website:
        # Home page, then career pages up to two links deep ("Careers" → "jobs.example.com"), at most MAX_PAGES;
        # links to a job board or a jobs./careers. host first, then the shortest paths.
        base = (urlparse(website).hostname or "").removeprefix("www.")
        pages, seen, i = [(website, _page(website))], {website}, 0
        while i < len(pages) and len(pages) < MAX_PAGES:
            src, html = pages[i]
            links = []
            for href in CAREER_LINK.findall(html):
                url = urljoin(src, href)
                h = urlparse(url).hostname or ""
                board = any(rx.search(url) for _, rx in BOARD_LINKS)
                if url not in seen and (h.endswith(base) or board):
                    seen.add(url)
                    links.append((not board, h.removeprefix("www.") == base, url.count("/"), url))
            for *_, url in sorted(links)[: MAX_PAGES - len(pages)]:
                pages.append((url, _page(url)))
            i += 1
        for url, page in pages:
            found = board_links(page)
            if "rmkcdn.successfactors.com" in page:  # the page itself is a SuccessFactors career site
                found.append(("successfactors", urlparse(url).hostname or ""))
            for ats, slug in found:
                if probe(ats, slug)[0]:  # linked by the company itself: right even while it has no openings
                    return ats, slug
    want = company_tokens(name)
    for slug in guesses(name):
        for ats in ("greenhouse", "lever", "ashby", "smartrecruiters", "personio", "recruitee", "workable"):
            ok, board_name, n = probe(ats, slug)
            # A guessed name can belong to another company (or be an abandoned account): it must list jobs, and where
            # the API names the company, it must be the same company.
            if ok and n and (not board_name or company_match(company_tokens(board_name), want) == "strong"):
                return ats, slug
    return None


# ---------- the model's part ----------

RATE_SCHEMA = {
    "type": "object",
    "properties": {"fits": {"type": "array", "items": {"type": "integer"}}},
    "required": ["fits"],
}
SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "companies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "website": {"type": "string"}},
                "required": ["name", "website"],
            },
        }
    },
    "required": ["companies"],
}


def _wanted_text(p) -> str:
    like = ", ".join(list(p.liked_companies.values())[:40]) or "(none named)"
    block = ", ".join(p.block_companies) or "(none)"
    return f"COMPANIES THE CANDIDATE WANTS TO WORK FOR, OR LIKE THESE: {like}\nNOT THESE: {block}"


def rate_prompt(p) -> str:
    return f"""You judge EMPLOYERS for one job seeker: is a company the same kind of employer as the ones they want
(industry, business model, size, clients, the kind of roles they would hire the candidate for)? Unknown companies:
judge from what they hire for and the snippet. Keep plausible fits; drop clearly different kinds of companies.

CANDIDATE:
{p.candidate}

{_wanted_text(p)}{p.examples_block()}

The user sends numbered companies. Answer with a single JSON object only:
{{"fits": [numbers of the fitting companies]}}."""


def suggest_prompt(p, n: int) -> str:
    return f"""You help one job seeker find employers. Name up to {n} real companies of the same kind as the ones they
want – similar business, clients and delivery model, likely to hire someone like the candidate where they can work.
Only companies you are sure exist; give each company's main website (https://…), or "" if you don't know it. Also give
the websites of the wanted companies listed below where you know them.

CANDIDATE:
{p.candidate}

{_wanted_text(p)}{p.examples_block()}

Answer with a single JSON object only: {{"companies": [{{"name": "...", "website": "https://..."}}]}}."""


def _snippet(j) -> str:
    return re.sub(r"\s+", " ", j.text or "")[:200]


def rate(p, jobs, store, conn, model, covered: set, cap: int) -> list[str]:
    """Ask the model about aggregator companies hiring where the person can work. Returns the accepted names."""
    verdicts = store.company_verdicts(p.id)
    found = {c["key"] for c in store.companies("found")}
    by_company: dict[str, list] = {}
    for j in jobs:
        k = company_key(j.company)
        if j.direct or not k or k in verdicts or k in covered or k in found or k in p.blocked_companies:
            continue
        if filters.location_class(p, j.location, j.text, j.remote):
            by_company.setdefault(k, []).append(j)
    items = list(by_company.items())[:cap]
    accepted = []
    for i in range(0, len(items), BATCH):
        chunk = items[i : i + BATCH]
        lines = []
        for n, (_, js) in enumerate(chunk, 1):
            titles = "; ".join(dict.fromkeys(j.title for j in js[:3]))
            lines.append(f"{n}. {js[0].company} – hiring: {titles} – {_snippet(js[0])}")
        try:
            raw = conn.chat_json(model, rate_prompt(p), "\n".join(lines), RATE_SCHEMA)
        except connectors.BadOutput as e:
            log.warning("[%s] company batch failed: %s", p.name, e)
            continue
        keep = {int(n) for n in raw.get("fits") or [] if str(n).strip().isdigit()}
        for n, (k, js) in enumerate(chunk, 1):
            store.set_company_verdict(p.id, k, js[0].company, n in keep, "model")
            if n in keep:
                store.add_company(k, js[0].company, origin="rated")
                accepted.append(js[0].company)
    store.commit()
    return accepted


def suggest(p, store, conn, model, n: int) -> list[str]:
    """New companies the model thinks are similar (plus websites for the wanted ones). Returns the new names."""
    try:
        raw = conn.chat_json(model, suggest_prompt(p, n), "Suggest now.", SUGGEST_SCHEMA)
    except connectors.BadOutput as e:
        log.warning("[%s] company suggestions failed: %s", p.name, e)
        return []
    verdicts, new = store.company_verdicts(p.id), []
    for c in (raw.get("companies") or [])[: n + len(p.liked_companies)]:
        name = str(c.get("name", "")).strip()[:80] if isinstance(c, dict) else ""
        k = company_key(name)
        if not k or k in p.blocked_companies:
            continue
        site = str(c.get("website", "")).strip()
        site = site if re.match(r"https?://[^\s/]+\.[a-z]{2,}", site, re.I) else ""
        is_new = store.company(k) is None
        store.add_company(k, name, site, origin="suggested")
        if k not in verdicts and k not in p.liked_companies:
            store.set_company_verdict(p.id, k, name, True, "model")
        if is_new:
            new.append(name)
    store.commit()
    return new


# ---------- one run ----------


def wanted_keys(p, store) -> set[str]:
    return set(p.liked_companies) | {k for k, v in store.company_verdicts(p.id).items() if v["fits"]}


def resolve_pending(store, wanted: set, covered: set, cap: int) -> tuple[list[dict], list[dict]]:
    """Look up job boards for wanted companies not looked up yet (or not found for RETRY_DAYS).
    Returns (found, not found)."""
    retry = (datetime.now(UTC) - timedelta(days=RETRY_DAYS)).isoformat(timespec="seconds")
    due = [
        c
        for c in store.companies()
        if c["key"] in wanted
        and (c["status"] == "pending" or (c["status"] == "none" and (c["checked_at"] or "") < retry))
    ][:cap]
    for c in [c for c in due if c["key"] in covered]:
        store.set_company_source(c["key"], "covered")  # already one of the configured sources
    due = [c for c in due if c["key"] not in covered]
    with cf.ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda c: resolve(c["name"], c["website"] or ""), due))
    found, missing = [], []
    for c, res in zip(due, results, strict=True):
        if res:
            store.set_company_source(c["key"], "found", *res)
            found.append({**c, "ats": res[0], "slug": res[1]})
        else:
            store.set_company_source(c["key"], "none")
            missing.append(c)
    store.commit()
    if due:
        log.info(
            "company boards: %d looked up, %d found: %s",
            len(due),
            len(found),
            "; ".join(f"{c['name']} ({c['ats']})" for c in found),
        )
    return found, missing


def discover(profiles, jobs, store, llm) -> tuple[dict, dict]:
    """Run discovery for every profile with wanted companies. Returns (source spec of newly found boards, per-profile
    report {profile id: {"rated", "suggested", "found", "unreadable": [names]}})."""
    covered = {company_key(j.company) for j in jobs if j.direct}
    report = {p.id: {"rated": [], "suggested": [], "found": [], "unreadable": []} for p in profiles}
    active = [p for p in profiles if p.liked_companies and p.companies("discover", True)]
    for p in active:
        for k, name in p.liked_companies.items():
            store.add_company(k, name, origin="liked")
    store.commit()
    if active and (picked := llm.get()):
        conn, model = picked
        for p in active:
            try:
                report[p.id]["rated"] = rate(p, jobs, store, conn, model, covered, p.companies("rate_max", 100))
                report[p.id]["suggested"] = suggest(p, store, conn, model, p.companies("suggest_max", 15))
            except connectors.Unavailable as e:
                log.warning("[%s] LLM went offline during company discovery: %s", p.name, e)
                break
            log.info(
                "[%s] companies: %d rated similar, %d new suggested",
                p.name,
                len(report[p.id]["rated"]),
                len(report[p.id]["suggested"]),
            )
    wanted = {p.id: wanted_keys(p, store) for p in active}
    cap = max([p.companies("resolve_max", 40) for p in active], default=0)
    found, missing = resolve_pending(store, set().union(*wanted.values()), covered, cap) if active else ([], [])
    spec: dict[str, list] = {}
    for c in found:
        spec.setdefault(c["ats"], []).append(c["slug"])
    for p in active:
        report[p.id]["found"] = [f"{c['name']} ({c['ats']})" for c in found if c["key"] in wanted[p.id]]
        report[p.id]["unreadable"] = [
            f"{c['name']} ({c['website']})" for c in missing if c["key"] in wanted[p.id] and c["website"]
        ]
    return spec, report
