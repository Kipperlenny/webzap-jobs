"""Rendering of the precision digest (templates/digest.html + .txt)."""

from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .common import source_label

_env = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parent / "templates"),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
SECTIONS = [
    ("strong", "Strong matches"),
    ("possible", "Possible matches – verify before applying"),
    ("contract", "Consulting / interim opportunities"),
]


def _salary(a: dict) -> str:
    if not (a.get("salary_min") or a.get("salary_max")):
        return "unknown"
    rng = "–".join(f"{int(x):,}" for x in (a.get("salary_min"), a.get("salary_max")) if x)
    return f"{rng} {a.get('salary_currency', '')} ({a.get('salary_type', '')}, {a.get('salary_confidence', '')})"


def _job(j) -> dict:
    a = j.assessment
    office = " · ".join(filter(None, [a.get("office_requirement"), a.get("travel_requirement")])) or "–"
    return {
        "title": j.title,
        "company": j.company,
        "url": j.url,
        "score": j.score,
        "one_line": a.get("one_line", ""),
        "facts": [
            ("Fit score", f"{j.score}/100"),
            ("Type", a.get("job_type", "")),
            ("Location", a.get("location_text") or j.location),
            ("Office / travel", office),
            ("Language", ", ".join(a.get("language_requirements", [])) or "–"),
            ("Compensation", _salary(a)),
        ],
        "why_fits": a.get("why_fits", []),
        "concerns": a.get("concerns", []),
        "flags": a.get("flags", []),
        "verify": a.get("verify_before_applying", []),
        "source": source_label(j),
    }


def render(
    p, picked: dict, notable: list, notable_rules: list, stats: dict, scanned: int, candidates: int, note: str
) -> tuple[str, str]:
    rejected = [
        {
            "company": j.company,
            "title": j.title,
            "url": j.url,
            "reason": j.assessment.get("exclusion_reason") or f"score {j.score}",
        }
        for j in notable
    ]
    rejected += [{"company": j.company, "title": j.title, "url": j.url, "reason": r} for j, r in notable_rules]
    ctx = {
        "today": datetime.now().strftime("%A, %d %B %Y"),
        "checked": datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
        "profile_name": p.name,
        "counts": {k: len(v) for k, v in picked.items()},
        "sections": [{"label": label, "jobs": [_job(j) for j in picked[key]]} for key, label in SECTIONS],
        "rejected": rejected,
        "none_message": p.out("none_message", "No verified strong-match roles found today."),
        "scanned": scanned,
        "candidates": candidates,
        "sources_ok": sum(1 for v in stats.values() if isinstance(v, int)),
        "sources_failed": [k for k, v in stats.items() if not isinstance(v, int)],
        "note": note,
    }
    return _env.get_template("digest.html").render(**ctx), _env.get_template("digest.txt").render(**ctx)
