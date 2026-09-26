"""Emails of the web app, rendered from templates/email/ (HTML + plain text) in the recipient's language. No accounts:
every email carries the tokenized links a person needs."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from jobagent import mail
from jobagent.common import source_parts
from jobagent.sources import Job

import extract
import golinks
import i18n
from matching import Match
from settings import BASE_URL, CONFIRM_TTL_HOURS, CONTACT_EMAIL, MAIL_FROM, REPO_URL

_env = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parent / "templates" / "email"),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.globals.update(base_url=BASE_URL, site=BASE_URL.split("//")[-1], repo_url=REPO_URL, ttl_hours=CONFIRM_TTL_HOURS)


def render(name: str, lang: str = i18n.DEFAULT, **ctx) -> tuple[str, str]:
    """(html, text) of templates/email/<name>.html and .txt."""
    html = _env.get_template(f"{name}.html").render(_=i18n.translator(lang), lang=lang, **ctx)
    text = _env.get_template(f"{name}.txt").render(_=i18n.translator(lang, markup=False), lang=lang, **ctx)
    return html, text


def _send(to: str, lang: str, subject: str, name: str, headers: dict | None = None, **ctx):
    html, text = render(name, lang, **ctx)
    mail.deliver(mail.build(subject, MAIL_FROM, to, text, html, reply_to=CONTACT_EMAIL, headers=headers))


def link(lang: str, path: str) -> str:
    """Absolute URL of a page in the person's language, e.g. https://webzap.com/de/manage/…"""
    return f"{BASE_URL}/{i18n.pick(lang)}{path}"


def subject(lang: str, key: str, **kw) -> str:
    return i18n.gettext(lang, f"email.{key}", markup=False, **kw)


def send_confirmation(to: str, tokens: dict, lang: str):
    _send(
        to,
        lang,
        subject(lang, "confirmation.subject"),
        "confirmation",
        confirm_url=link(lang, f"/confirm/{tokens['confirm']}"),
        manage_url=link(lang, f"/manage/{tokens['manage']}"),
        delete_url=link(lang, f"/delete/{tokens['delete']}"),
    )


def send_welcome(to: str, token: str, lang: str):
    """Right after confirmation: what happens now, and the manage link again."""
    _send(to, lang, subject(lang, "welcome.subject"), "welcome", manage_url=link(lang, f"/manage/{token}"))


def send_manage_link(to: str, token: str, lang: str):
    _send(to, lang, subject(lang, "manage_link.subject"), "manage_link", manage_url=link(lang, f"/manage/{token}"))


def help_box(derived: dict | None, token: str, lang: str) -> dict | None:
    """Context for the 'help us understand you' box, or None when the text was clear enough."""
    if not extract.needs_help(derived):
        return None
    default = i18n.gettext(lang, "email.digest.default_question", markup=False)
    return {
        "url": link(lang, f"/manage/{token}#improve"),
        "questions": (derived.get("questions") or [default])[:3],
        "guess": (derived.get("assumptions") or [""])[0],
    }


def digest_item(job, match, lang: str, feedback_url: str, sponsor_id: str = "") -> dict:
    """One job as the digest shows it: why it matches and where it comes from, in the person's language. Its link is
    counted anonymously per source (golinks)."""
    sponsored = bool(sponsor_id)
    _ = i18n.translator(lang, markup=False)
    why = [_("email.digest.why_role", role=match.role)]
    if match.where:
        where = _("email.digest.worldwide") if match.where == "worldwide" else match.where
        why.append(_("email.digest.why_remote", where=where) if match.loc_kind == "remote" else where)
    if match.hits:
        why.append(_("email.digest.why_mentions", words=", ".join(match.hits)))
    kind, name = source_parts(job)
    return {
        "title": job.title,
        "company": job.company,
        "location": (job.location or "")[:80],
        "url": golinks.link(job.url, *_click_source(job, sponsor_id)),
        "why": " · ".join(why),
        "source": _("email.digest.src_sponsored") if sponsored else _(f"email.digest.src_{kind}", name=name),
        "sponsored": sponsored,
        "partner": job.partner,
        "feedback_url": feedback_url,
    }


def _click_source(job, sponsor_id: str) -> tuple[str, str]:
    if sponsor_id:
        return "sponsored", sponsor_id
    if job.partner:
        return "partner", job.partner
    return "job", job.source


def external_item(x: dict) -> dict:
    """An external link (external.suggest) as the digest shows it: its link counted per site."""
    return x | {"url": golinks.link(x["url"], "external", x["id"])}


def digest_subject(lang: str, n: int) -> str:
    if not n:
        return subject(lang, "digest.subject_help")
    return subject(lang, "digest.subject_one") if n == 1 else subject(lang, "digest.subject_many", n=n)


def send_digest(to: str, lang: str, token: str, items: list[dict], **ctx):
    """Job digest with one-click unsubscribe headers (RFC 8058), as mailbox providers expect for bulk mail."""
    unsubscribe_url = link(lang, f"/unsubscribe/{token}")
    headers = {
        "List-Unsubscribe": f"<{unsubscribe_url}>, <mailto:{CONTACT_EMAIL}?subject=unsubscribe>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    _send(
        to,
        lang,
        digest_subject(lang, len(items)),
        "digest",
        headers=headers,
        items=items,
        manage_url=link(lang, f"/manage/{token}"),
        unsubscribe_url=unsubscribe_url,
        **ctx,
    )


# Made-up jobs for the sample email on the website (/sample). They show every label a real digest can carry.
SAMPLE_JOBS = [
    (
        Job(
            "greenhouse", "Greenfield Foods", "Senior Brand Manager – Plant-based", "#", location="Hamburg", direct=True
        ),
        Match(0, "", "Senior Brand Manager", "local", "Hamburg", ("brand strategy", "sustainability")),
        False,
    ),
    (
        Job("jooble", "Northwind Outdoor", "Head of Content Marketing", "#", location="Remote", partner="Jooble"),
        Match(0, "", "Content Marketing Lead", "remote", "Germany", ("content",)),
        False,
    ),
    (
        Job("sponsored", "Kaleo Cosmetics", "Brand Strategy Lead", "#", location="Hamburg / hybrid", direct=True),
        Match(0, "", "Brand Strategy Lead", "local", "Hamburg", ("consumer brand",)),
        True,
    ),
]


def render_sample(lang: str) -> tuple[str, str]:
    """The real digest template, filled with SAMPLE_JOBS – so the sample always looks like what people get."""
    items = [digest_item(job, m, lang, "#", sponsor_id="sample" if sp else "") for job, m, sp in SAMPLE_JOBS]
    return render(
        "digest",
        lang,
        items=items,
        intro=i18n.gettext(lang, "sample.intro", markup=False),
        help=None,
        frequency="weekly",
        disclosure=True,
        externals=[{"name": "StepStone", "url": "#", "query": "Brand Manager", "where": "Hamburg"}],
        manage_url="#",
        unsubscribe_url="#",
    )
