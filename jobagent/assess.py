"""Model-based assessment of one posting against a precision profile.

The model returns facts and per-category points; the total score and the classification are computed here, so the
rubric in the profile – not the model's mood – decides what reaches the digest.
"""

from . import connectors

ASSESS_VERSION = 2

SALARY_CONFIDENCE = ["stated", "estimated-high-confidence", "estimated-low-confidence", "unknown"]


def schema(p) -> dict:
    cats = {c["name"]: {"type": "integer", "minimum": 0, "maximum": int(c["points"])} for c in p.categories}
    str_list = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": {
            "employer_identified": {"type": "boolean"},
            "is_specific_job": {"type": "boolean"},
            "job_type": {"type": "string", "enum": ["permanent", "contract", "interim", "freelance"]},
            "location_eligibility": {"type": "string", "enum": ["explicit", "likely", "unclear", "ineligible"]},
            "location_text": {"type": "string"},
            "office_requirement": {"type": "string"},
            "travel_requirement": {"type": "string"},
            "language_requirements": str_list,
            "salary_min": {"type": ["number", "null"]},
            "salary_max": {"type": ["number", "null"]},
            "salary_currency": {"type": "string"},
            "salary_type": {"type": "string", "enum": ["base", "total_comp", "day_rate", "unknown"]},
            "salary_confidence": {"type": "string", "enum": SALARY_CONFIDENCE},
            "category_scores": {"type": "object", "properties": cats, "required": list(cats)},
            "penalties_applied": {
                "type": "array",
                "items": {"type": "string", "enum": [x["name"] for x in p.penalties] or ["none"]},
            },
            "exclusion_reason": {"type": "string"},
            "flags": str_list,
            "why_fits": str_list,
            "concerns": str_list,
            "verify_before_applying": str_list,
            "one_line": {"type": "string"},
        },
        "required": [
            "employer_identified",
            "is_specific_job",
            "job_type",
            "location_eligibility",
            "location_text",
            "office_requirement",
            "travel_requirement",
            "language_requirements",
            "salary_min",
            "salary_max",
            "salary_currency",
            "salary_type",
            "salary_confidence",
            "category_scores",
            "penalties_applied",
            "exclusion_reason",
            "flags",
            "why_fits",
            "concerns",
            "verify_before_applying",
            "one_line",
        ],
    }


def system_prompt(p) -> str:
    rules = p.raw.get("rules", {})
    comp = p.raw.get("compensation", {})
    cats = "\n".join(f"- {c['name']} (0-{c['points']}): {c.get('guidance', '')}" for c in p.categories)
    pens = "\n".join(f"- {x['name']} (-{x['points']}): {x['condition']}" for x in p.penalties) or "- none"
    excl = "\n".join(f"- {x}" for x in rules.get("exclude", [])) or "- none"
    flags = "\n".join(f"- {x}" for x in rules.get("flag", [])) or "- none"
    comp_lines = "\n".join(f"{k}: {v}" for k, v in comp.items() if k != "guidance")
    return f"""You assess ONE job posting for ONE candidate. Be factual, skeptical and conservative about eligibility.
Never invent facts. Estimated salaries must be labelled as estimates. Never assume "remote Europe" includes the
candidate's country unless the posting says so or it is clearly implied.

CANDIDATE:
{p.candidate}

COMPENSATION:
{comp_lines}
{comp.get("guidance", "")}

SCORING – give each category 0..max points:
{cats}

PENALTIES – list the names of those that apply (code subtracts the points):
{pens}

EXCLUDE the posting (put the reason in exclusion_reason, otherwise "") if:
{excl}

FLAG (add to "flags" when it applies):
{flags}

Also: employer_identified=false for recruitment agencies hiding the client; is_specific_job=false for category or list
pages and talent pools. Fill office_requirement/travel_requirement with the exact wording from the posting ("not
stated" if absent). Salary: use stated numbers (salary_confidence "stated"); otherwise estimate if you can and label it
"estimated-high-confidence" / "estimated-low-confidence", else null with "unknown".
If an EXCLUDE rule clearly applies, exclusion_reason MUST say which – and one_line must agree with it. If it only
might apply (unclear from the posting), do not exclude: add it to verify_before_applying instead.
why_fits / concerns / verify_before_applying: short, concrete bullet sentences in English. one_line: a 1-sentence
verdict. Answer with a single JSON object only."""


def assess(conn, model, p, job) -> dict:
    user = (
        f"TITLE: {job.title}\nCOMPANY: {job.company}\nLOCATION: {job.location} (remote flag: {job.remote})\n"
        f"EMPLOYMENT TYPE: {job.employment_type}\nSOURCE: {'employer ATS' if job.direct else job.source}\n"
        f"STATED SALARY: {job.salary_min}-{job.salary_max} {job.currency or ''}\nPOSTED: {job.posted}\n"
        f"URL: {job.url}\n\nDESCRIPTION:\n{job.text[:9000]}"
    )
    raw = conn.chat_json(model, system_prompt(p), user, schema(p))
    return normalise(p, raw)


def normalise(p, a: dict) -> dict:
    scores = a.get("category_scores") or {}
    clean = {}
    for c in p.categories:
        try:
            clean[c["name"]] = max(0, min(int(c["points"]), int(scores.get(c["name"], 0))))
        except (TypeError, ValueError):
            clean[c["name"]] = 0
    a["category_scores"] = clean
    names = {x["name"]: int(x["points"]) for x in p.penalties}
    a["penalties_applied"] = [n for n in (a.get("penalties_applied") or []) if n in names]
    total = sum(clean.values()) - sum(names[n] for n in a["penalties_applied"])
    a["total"] = max(0, round(100 * total / p.max_points)) if p.max_points else 0
    for k in ("flags", "why_fits", "concerns", "verify_before_applying", "language_requirements"):
        a[k] = [str(x) for x in (a.get(k) or []) if x]
    a["v"] = ASSESS_VERSION
    return a


def classify(p, job, a: dict) -> tuple[str, str]:
    """Return (status, reason): strong | possible | contract | rejected."""
    if a.get("exclusion_reason"):
        return "rejected", a["exclusion_reason"]
    if not a.get("employer_identified", True):
        return "rejected", "employer not identified"
    if not a.get("is_specific_job", True):
        return "rejected", "not a specific job posting"
    if a.get("location_eligibility") == "ineligible":
        return "rejected", "location not eligible"
    total = a["total"]
    if a.get("job_type") in ("contract", "interim", "freelance") or job.contract:
        return ("contract", "") if total >= p.out("possible_min", 60) else ("rejected", f"score {total}")
    if total >= p.out("strong_min", 75) and a.get("location_eligibility") == "explicit":
        return "strong", ""
    if total >= p.out("possible_min", 60):
        return "possible", ""
    return "rejected", f"score {total}"


def pick_connector():
    return connectors.pick(connectors.configured())
