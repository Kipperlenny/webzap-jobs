"""Languages of the site and its emails. Strings live in locales/<lang>.toml; English is the fallback.

Pages get their language from the URL prefix (/es/, /fr/, /de/ – see LangPrefix) or, without one, from the browser's
Accept-Language header. Emails use the language stored with the sign-up. No cookies involved.
"""

import re
import tomllib
from functools import partial
from pathlib import Path

from markupsafe import Markup

LANGS = ("en", "es", "fr", "de")
DEFAULT = "en"
NAMES = {"en": "English", "es": "Spanish", "fr": "French", "de": "German"}  # for the language model's prompt


def _flatten(d: dict, prefix: str = "") -> dict[str, str]:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out |= _flatten(v, f"{prefix}{k}.")
        else:
            out[f"{prefix}{k}"] = v
    return out


_DIR = Path(__file__).resolve().parent / "locales"
STRINGS = {lang: _flatten(tomllib.loads((_DIR / f"{lang}.toml").read_text())) for lang in LANGS}


def pick(lang: str | None) -> str:
    return lang if lang in LANGS else DEFAULT


def gettext(lang: str, key: str, markup: bool = True, **kw):
    """The string for key in lang (English if missing). Strings may contain HTML: as Markup, arguments are escaped."""
    s = STRINGS[pick(lang)].get(key) or STRINGS[DEFAULT][key]
    # Markup is safe here: the strings come from our own locale files, never from users.
    if isinstance(s, list):
        return [Markup(x) if markup else x for x in s]  # noqa: S704
    if not markup:
        return s.format(**kw) if kw else s
    return Markup(s).format(**kw) if kw else Markup(s)  # noqa: S704


def translator(lang: str, markup: bool = True):
    """`_` for templates: _("key", name=value). Use markup=False for plain-text emails."""
    return partial(gettext, pick(lang), markup=markup)


def negotiate(accept_language: str | None) -> str:
    """Best supported language from an Accept-Language header, e.g. 'de-DE,de;q=0.9,en;q=0.8' → 'de'."""
    ranked = []
    for i, part in enumerate((accept_language or "").split(",")):
        m = re.match(r"\s*([a-zA-Z]{1,8})(?:-[\w-]*)?\s*(?:;\s*q=([\d.]+))?", part)
        if m:
            try:
                q = float(m.group(2) or 1)
            except ValueError:
                q = 0
            ranked.append((-q, i, m.group(1).lower()))
    return next((lang for q, _, lang in sorted(ranked) if q < 0 and lang in LANGS), DEFAULT)


class LangPrefix:
    """ASGI middleware: /de/manage/x → route /manage/x with scope["state"]["lang"] = "de". Routes stay language-free."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            head, _, rest = scope["path"].lstrip("/").partition("/")
            if head in LANGS:
                scope = {**scope, "path": "/" + rest, "state": {**scope.get("state", {}), "lang": head}}
        await self.app(scope, receive, send)
