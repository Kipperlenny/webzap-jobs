"""Outgoing links in our emails go through /go, which adds one click to a daily total per link kind and source
(e.g. "external · stepstone-de: 12") and forwards to the target. Nothing about the person is involved: the link is
the same for every recipient, and nothing but the total is stored. The signature makes sure /go only forwards to
links we put into an email ourselves – it is not an open redirect.
"""

import hashlib
import hmac
import re
from urllib.parse import urlencode

from settings import BASE_URL, HASH_PEPPER

KINDS = ("job", "partner", "sponsored", "external")
_KEY = hmac.new(HASH_PEPPER, b"go-links", hashlib.sha256).digest()  # derived: no extra secret to manage


def clean_source(source: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", (source or "").lower()).strip("-")[:40] or "unknown"


def _sig(url: str, kind: str, source: str) -> str:
    return hmac.new(_KEY, f"{kind}\n{source}\n{url}".encode(), hashlib.sha256).hexdigest()[:32]


def link(url: str, kind: str, source: str) -> str:
    """The counted link for an email. Anything that isn't an http(s) link (e.g. '#' in the sample) stays as it is."""
    if kind not in KINDS:
        raise ValueError(kind)
    if not url.startswith(("https://", "http://")):
        return url
    source = clean_source(source)
    return f"{BASE_URL}/go?" + urlencode({"u": url, "k": kind, "s": source, "h": _sig(url, kind, source)})


def verify(url: str, kind: str, source: str, sig: str) -> bool:
    return (
        kind in KINDS
        and url.startswith(("https://", "http://"))
        and source == clean_source(source)
        and hmac.compare_digest(sig, _sig(url, kind, source))
    )
