"""WebZap Jobs sign-up: email + free-text wish, double opt-in, one-click data deletion. No cookies, no trackers."""

import asyncio
import hashlib
import inspect
import json
import logging
import re
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader, PrefixLoader
from starlette.concurrency import run_in_threadpool

import i18n
import mailer
import store
from settings import (
    BASE_URL,
    CONTACT_EMAIL,
    GIT_COMMIT,
    LEGAL_BINDING_LANG,
    LEGAL_DIR,
    MAX_TEXT,
    MIN_TEXT,
    REPO_URL,
)

log = logging.getLogger("webzap-jobs")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Hourly purge of unconfirmed sign-ups older than the confirmation window."""

    async def purge_loop():
        while True:
            await run_in_threadpool(store.purge_expired)
            await asyncio.sleep(3600)

    missing = [f"{n}.{lang}.html" for n in LEGAL_PAGES for lang in i18n.LANGS if not _legal_file(n, lang).exists()]
    if missing:
        log.warning("legal pages missing in %s: %s", LEGAL_DIR, ", ".join(missing))
    task = asyncio.create_task(purge_loop())
    yield
    task.cancel()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
HERE = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
# "legal/…" is looked up in the instance's LEGAL_DIR first (imprint, privacy policy), then in the repo's templates.
templates.env.loader = ChoiceLoader([PrefixLoader({"legal": FileSystemLoader(LEGAL_DIR)}), templates.env.loader])
templates.env.globals.update(
    base_url=BASE_URL,
    repo_url=REPO_URL,
    contact=CONTACT_EMAIL,
    commit=GIT_COMMIT,
    frequencies=store.FREQUENCIES,
    langs=i18n.LANGS,
    legal_binding=LEGAL_BINDING_LANG,
    max_text=MAX_TEXT,
)
templates.env.filters["ts"] = lambda t: time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t)) if t else "–"
# The landing page shows the real function that stores sign-ups, read from the running code.
STORE_SNIPPET = inspect.getsource(store.add_pending)
STORE_LINE = inspect.getsourcelines(store.add_pending)[1]

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}\.[a-z]{2,}$", re.I)
_hits: dict[str, deque] = defaultdict(deque)
RATE = (5, 3600)  # max 5 sign-ups per IP per hour


def _rate_limited(request: Request) -> bool:
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
    key = hashlib.sha256(ip.encode()).hexdigest()  # never keep raw IPs in memory longer than needed
    now = time.time()
    for k in [k for k, q in _hits.items() if not q or q[-1] < now - RATE[1]]:  # keep memory bounded
        del _hits[k]
    q = _hits[key]
    while q and q[0] < now - RATE[1]:
        q.popleft()
    if len(q) >= RATE[0]:
        return True
    q.append(now)
    return False


CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", CSP)  # a route may set a stricter one (see sample_mail)
    if not _url_lang(request):
        resp.headers["Vary"] = "Accept-Language"  # without /xx/ prefix, the language comes from the browser
    resp.headers.update(
        {
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "interest-cohort=(), camera=(), microphone=(), geolocation=()",
            "Cache-Control": "no-store",
        }
    )
    if request.url.path.startswith(("/manage/", "/confirm/", "/delete/", "/feedback/", "/unsubscribe/")):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    if request.headers.get("x-forwarded-proto") == "https":
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


def _url_lang(request: Request) -> str | None:
    return request.scope.get("state", {}).get("lang")  # set by i18n.LangPrefix for /es/…, /fr/…, /de/…


def lang_of(request: Request) -> str:
    return _url_lang(request) or i18n.negotiate(request.headers.get("accept-language"))


def page(request, name, status=200, **ctx):
    lang = lang_of(request)
    ctx |= {
        "lang": lang,
        "_": i18n.translator(lang),
        "url": lambda path, to=lang: f"/{to}{path}",  # link within the site, keeping the language
        "path": request.url.path,  # without language prefix, for the language switcher
    }
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def errors_for(request):
    return i18n.translator(lang_of(request), markup=False)


def index_page(request, status=200, **ctx):
    return page(request, "index.html", status, snippet=STORE_SNIPPET, snippet_line=STORE_LINE, **ctx)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, c: str = ""):
    # ?c=<campaign> on ad links: counted as a plain number per day, nothing about the visitor (see privacy policy).
    if campaign := store.campaign_name(c):
        store.count_visit(campaign)
    return index_page(request, campaign=campaign)


@app.post("/signup", response_class=HTMLResponse)
async def signup(
    request: Request,
    email: str = Form(""),
    wish: str = Form(""),
    consent: str = Form(""),
    frequency: str = Form("weekly"),
    website: str = Form(""),
    campaign: str = Form(""),
):
    email, wish, lang, campaign = email.strip(), wish.strip(), lang_of(request), store.campaign_name(campaign)
    _ = errors_for(request)
    errors = []
    if frequency not in ("daily", "weekly"):
        frequency = "weekly"
    if not EMAIL_RE.match(email) or len(email) > 254:
        errors.append(_("errors.email"))
    if len(wish) < MIN_TEXT:
        errors.append(_("errors.too_short"))
    if len(wish) > MAX_TEXT:
        errors.append(_("errors.too_long", max=MAX_TEXT))
    if not consent:
        errors.append(_("errors.consent"))
    form = {"email": email, "wish": wish, "frequency": frequency, "campaign": campaign}
    if errors:
        return index_page(request, 400, errors=errors, **form)
    # Honeypot filled or too many requests: pretend success, do nothing.
    # Also caps confirmation emails per address (2 per 10 min, 3 per day) so nobody can flood someone's inbox.
    if (
        website
        or _rate_limited(request)
        or await run_in_threadpool(store.recent_pending, email, 600) >= 2
        or await run_in_threadpool(store.recent_pending, email, 86400) >= 3
    ):
        return page(request, "check_email.html")
    tokens = await run_in_threadpool(store.add_pending, email, wish, frequency, lang, campaign)
    try:
        await run_in_threadpool(mailer.send_confirmation, email, tokens, lang)
    except Exception:  # noqa: BLE001
        log.exception("sending confirmation failed")
        await run_in_threadpool(store.delete, tokens["delete"])
        return index_page(request, 502, errors=[_("errors.send_failed")], **form)
    return page(request, "check_email.html")


# Links in emails only show a button; the state change is a POST, so mail scanners that prefetch links can't
# confirm or delete anything.
@app.get("/confirm/{token}", response_class=HTMLResponse)
def confirm_page(request: Request, token: str):
    ok = store.token_valid("confirm", token)
    return page(request, "action.html", 200 if ok else 404, ok=ok, action="confirm", token=token)


@app.post("/confirm/{token}", response_class=HTMLResponse)
def confirm_do(request: Request, token: str):
    who = store.confirm(token)
    if who:
        try:
            mailer.send_welcome(who["email"], who["manage"], who["lang"])
        except Exception:  # noqa: BLE001 – the sign-up is confirmed anyway; the confirmation email has the links
            log.exception("sending welcome email failed")
    return page(request, "done.html", 200 if who else 404, ok=bool(who), action="confirm")


@app.get("/delete/{token}", response_class=HTMLResponse)
def delete_page(request: Request, token: str):
    ok = store.token_valid("delete", token)
    return page(request, "action.html", 200 if ok else 404, ok=ok, action="delete", token=token)


@app.post("/delete/{token}", response_class=HTMLResponse)
def delete_do(request: Request, token: str):
    ok = store.delete(token)
    return page(request, "done.html", 200 if ok else 404, ok=ok, action="delete")


# ---------- manage: everything via the tokenized link from the email ----------


@app.get("/manage", response_class=HTMLResponse)
def manage_request_page(request: Request):
    return page(request, "manage_request.html")


@app.post("/manage", response_class=HTMLResponse)
async def manage_request(request: Request, email: str = Form(""), website: str = Form("")):
    email = email.strip()
    if EMAIL_RE.match(email) and not website and not _rate_limited(request):
        token = await run_in_threadpool(store.new_manage_token, email)
        if token:
            try:
                await run_in_threadpool(mailer.send_manage_link, email, token, lang_of(request))
            except Exception:  # noqa: BLE001
                log.exception("sending manage link failed")
    # Same answer whether or not the address exists – no way to probe who signed up.
    return page(request, "check_email.html", lost_link=True)


def _manage(request, token, status=200, **ctx):
    data = store.get_by_manage(token)
    if not data:
        return page(request, "action.html", 404, ok=False, action="manage")
    return page(request, "manage.html", status, token=token, d=data, **ctx)


@app.get("/manage/{token}", response_class=HTMLResponse)
def manage_page(request: Request, token: str):
    return _manage(request, token)


@app.post("/manage/{token}", response_class=HTMLResponse)
def manage_update(
    request: Request, token: str, wish: str = Form(""), frequency: str = Form(""), email_lang: str = Form("")
):
    wish, _ = wish.strip(), errors_for(request)
    if frequency not in store.FREQUENCIES:
        return _manage(request, token, 400, errors=[_("errors.frequency")])
    if not MIN_TEXT <= len(wish) <= MAX_TEXT:
        return _manage(request, token, 400, errors=[_("errors.length", min=MIN_TEXT, max=MAX_TEXT)])
    if not store.update(token, wish, frequency, i18n.pick(email_lang)):
        return page(request, "action.html", 404, ok=False, action="manage")
    return _manage(request, token, saved=True)


@app.get("/manage/{token}/export")
def manage_export(token: str):
    data = store.get_by_manage(token)
    if not data:
        return PlainTextResponse("not found", status_code=404)
    return Response(
        json.dumps(data, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="webzap-jobs-my-data.json"'},
    )


@app.post("/manage/{token}/delete", response_class=HTMLResponse)
def manage_delete(request: Request, token: str):
    ok = store.delete_by_manage(token)
    return page(request, "done.html", 200 if ok else 404, ok=ok, action="delete")


# ---------- digest links: feedback and unsubscribe ----------

# Stored in English (store.feedback_signals reads them); the form shows them translated via feedback.reasons.<key>.
FEEDBACK_REASONS = {
    "field": "not my field",
    "location": "wrong location",
    "junior": "too junior",
    "senior": "too senior",
    "salary": "salary too low",
    "company": "not this company",
    "known": "already applied / known",
    "other": "other",
}


# GET only shows a page (mail scanners prefetch links); the vote is stored by the POST.
@app.get("/feedback/{token}", response_class=HTMLResponse)
def feedback_page(request: Request, token: str, v: str = "up"):
    job = store.feedback_get(token)
    if not job:
        return page(request, "action.html", 404, ok=False, action="feedback")
    return page(
        request, "feedback.html", token=token, job=job, vote="down" if v == "down" else "up", reasons=FEEDBACK_REASONS
    )


@app.post("/feedback/{token}", response_class=HTMLResponse)
async def feedback_save(request: Request, token: str):
    form = await request.form()
    vote = "down" if form.get("vote") == "down" else "up"
    # Only predefined reasons – no free text, so no personal data ends up in feedback.
    reason = "; ".join(r for r in form.getlist("reason") if r in FEEDBACK_REASONS.values())
    if not store.feedback_set(token, vote, reason):
        return page(request, "action.html", 404, ok=False, action="feedback")
    return page(request, "done.html", ok=True, action="feedback", vote=vote)


@app.get("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_page(request: Request, token: str):
    ok = store.signup_id_by_manage(token) is not None
    return page(request, "action.html", 200 if ok else 404, ok=ok, action="unsubscribe", token=token)


@app.post("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_do(request: Request, token: str):
    # Also the RFC 8058 one-click endpoint: mail providers POST here directly, so no confirmation step.
    ok = store.unsubscribe(token)
    return page(request, "done.html", 200 if ok else 404, ok=ok, action="unsubscribe", token=token)


LEGAL_PAGES = ("imprint", "privacy")


def _legal_file(name: str, lang: str) -> Path:
    return LEGAL_DIR / f"{name}.{lang}.html"


def legal_page(request: Request, name: str):
    """The page in the visitor's language, else the binding one, else English; a neutral notice if there is none."""
    for lang in dict.fromkeys((lang_of(request), LEGAL_BINDING_LANG, i18n.DEFAULT)):
        if lang and _legal_file(name, lang).exists():
            return page(request, f"legal/{name}.{lang}.html", page_name=name, page_lang=lang)
    return page(request, "legal/missing.html", 404, page_name=name)


@app.get("/imprint", response_class=HTMLResponse)
def imprint(request: Request):
    return legal_page(request, "imprint")


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return legal_page(request, "privacy")


# ---------- sample email: the real digest template with made-up jobs ----------


@app.get("/sample", response_class=HTMLResponse)
def sample(request: Request):
    return page(request, "sample.html", subject=mailer.digest_subject(lang_of(request), len(mailer.SAMPLE_JOBS)))


@app.get("/sample/mail", response_class=HTMLResponse)
def sample_mail(request: Request):
    """Shown in an iframe on /sample. Emails need inline styles, so this page alone allows them – and nothing else."""
    html, _ = mailer.render_sample(lang_of(request))
    csp = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'; base-uri 'none'"
    return HTMLResponse(
        f'<!doctype html><meta charset="utf-8"><body style="margin:16px">{html}',
        headers={
            "Content-Security-Policy": csp,
        },
    )


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


app.add_middleware(i18n.LangPrefix)  # outermost: strips /es/, /fr/, /de/ before anything else sees the path
