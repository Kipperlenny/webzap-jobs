"""Job titles the profile's [titles] patterns don't cover: the model decides once per title whether it could be one of
the candidate's roles. Verdicts are stored, so the hard list grows by itself and each title costs one line in a batch.
"""

import hashlib
import logging
import re

from . import connectors

log = logging.getLogger("jobagent")

BATCH = 60
GENDER = re.compile(
    r"\(\s*(?:[mwfdxh]\s*[/|,]\s*){1,3}[mwfdxh]\s*\)|\b(?:[mwfdxh]/){1,3}[mwfdxh]\b|\((?:all genders?|gn\*?|w\*m\*d)\)",
    re.I,
)
SCHEMA = {
    "type": "object",
    "properties": {"fits": {"type": "array", "items": {"type": "integer"}}},
    "required": ["fits"],
}


def norm(title: str) -> str:
    """The key a verdict is stored under: case, gender tags and spacing don't make another title."""
    t = GENDER.sub(" ", title or "").lower()
    return re.sub(r"\s+", " ", t).strip(" -–|,·")


def fingerprint(p) -> str:
    """Verdicts hold while the candidate description is unchanged."""
    return hashlib.sha256(p.candidate.encode()).hexdigest()[:16]


def system_prompt(p) -> str:
    patterns = "\n".join(f"- {rx.pattern}" for rx in p.title_include)
    return f"""You pre-screen job TITLES for one candidate. A later step reads each full posting, so keep a title if it
could plausibly be one of the candidate's target roles: the same kind of work and seniority under another name, in
another language, or at another kind of employer. Drop titles that are clearly a different job (another profession,
clearly junior, clearly a role the candidate description rules out).

CANDIDATE:
{p.candidate}

TITLE PATTERNS THE CANDIDATE ALREADY SEARCHES FOR (regular expressions):
{patterns}

The user sends numbered titles. Answer with a single JSON object only: {{"fits": [numbers of the titles to keep]}}.\
{p.examples_block()}"""


def judge(conn, model, p, titles: dict[str, str]) -> dict[str, bool]:
    """{key: title as posted} → {key: fits}. A batch the model garbles stays undecided and is asked again next run."""
    items, out = list(titles.items()), {}
    for i in range(0, len(items), BATCH):
        chunk = items[i : i + BATCH]
        user = "\n".join(f"{n}. {title}" for n, (_, title) in enumerate(chunk, 1))
        try:
            raw = conn.chat_json(model, system_prompt(p), user, SCHEMA)
        except connectors.BadOutput as e:
            log.warning("[%s] title batch failed: %s", p.name, e)
            continue
        keep = {int(n) for n in raw.get("fits") or [] if str(n).strip().isdigit()}
        out.update({key: n in keep for n, (key, _) in enumerate(chunk, 1)})
    return out
