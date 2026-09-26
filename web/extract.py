"""Task: turn a free-text "I want to…" brainstorm into structured job-search fields."""

from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

import i18n

PROMPT_VERSION = 5  # 5: person-facing fields in the person's language

Seniority = Literal["entry", "junior", "mid", "senior", "lead", "manager", "director", "executive", "any"]
WorkMode = Literal["onsite", "hybrid", "remote"]
EmploymentType = Literal["permanent", "contract", "freelance", "part-time", "internship", "apprenticeship"]


class Place(BaseModel):
    place: str = Field(description="City or region, as written, e.g. 'Hamburg'")
    country: str = Field(default="", description="Country in English, e.g. 'Germany'")


class Salary(BaseModel):
    minimum: float | None = Field(default=None, description="Lowest acceptable amount, as a number")
    currency: str = Field(default="", description="ISO code, e.g. EUR")
    period: Literal["year", "month", "day", "hour", ""] = ""


class SearchProfile(BaseModel):
    summary: str = Field(
        description="One or two sentences addressed to the person ('You're looking for …') "
        "describing our understanding. No names, no personal details."
    )
    intro: str = Field(
        default="",
        description="Opening of our first email to the person, 1-3 sentences, addressed to them: shows we really "
        "read their text. Match their tone (see rules).",
    )
    clarity: int = Field(
        default=3,
        ge=1,
        le=5,
        description="How well the text supports job matching: 1 = no idea "
        "what they want, 3 = workable with guesses, 5 = clear",
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description="Each guess we made beyond what was written, addressed to the person: 'We assumed …' (0-5)",
    )
    questions: list[str] = Field(
        default_factory=list,
        description="If clarity <= 3: 1-3 short, friendly questions to the person that would improve matching most",
    )
    target_roles: list[str] = Field(
        default_factory=list, description="Job titles to search for, most wanted first, incl. common synonyms (3-10)"
    )
    seniority: list[Seniority] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list, description="Professional fields / functions, e.g. 'marketing'")
    skills: list[str] = Field(default_factory=list, description="Skills and topics they want to use")
    industries_preferred: list[str] = Field(default_factory=list)
    industries_excluded: list[str] = Field(default_factory=list)
    locations: list[Place] = Field(default_factory=list, description="Places where they can work on-site/hybrid")
    work_modes: list[WorkMode] = Field(default_factory=list, description="Acceptable modes; empty = not stated")
    remote_regions: list[str] = Field(
        default_factory=list,
        description="Regions they may work remotely from/for, e.g. 'Europe', 'Germany', 'worldwide'",
    )
    relocation: bool | None = Field(default=None, description="Willing to relocate? null = not stated")
    travel: str = Field(default="", description="Travel willingness as stated, e.g. 'monthly'")
    employment_types: list[EmploymentType] = Field(default_factory=list)
    salary: Salary = Field(default_factory=Salary)
    languages: list[str] = Field(default_factory=list, description="Working languages they can use")
    company_preferences: list[str] = Field(
        default_factory=list, description="e.g. 'start-up', 'remote-first', 'sustainable', 'no agencies'"
    )
    must_have: list[str] = Field(default_factory=list)
    deal_breakers: list[str] = Field(default_factory=list, description="Things they explicitly do NOT want")
    keywords: list[str] = Field(default_factory=list, description="Words that make a posting more relevant")
    search_queries: list[str] = Field(default_factory=list, description="3-8 short queries for job-board search")
    missing_info: list[str] = Field(default_factory=list, description="Deprecated, leave empty")

    @field_validator("clarity", mode="before")
    @classmethod
    def _clamp_clarity(cls, v):
        try:
            return min(5, max(1, int(v)))
        except (TypeError, ValueError):
            return 3

    @field_validator("*", mode="before")
    @classmethod
    def _none_to_default(cls, v, info):
        # Models often write null for "not stated"; keep null only where it's meaningful.
        if v is None and info.field_name not in ("relocation",):
            field = cls.model_fields[info.field_name]
            return field.get_default(call_default_factory=True)
        return v


SCHEMA = SearchProfile.model_json_schema()

SYSTEM = """You turn a job seeker's free-text brainstorm into structured search criteria for a job-matching service.
Most people write only once, quickly and vaguely – make the most of it:
- Interpret generously and creatively. When the text is vague, infer the most plausible job-search intent from every
  hint (interests, past roles, tone, wording) and fill target_roles, fields, seniority etc. with reasonable guesses,
  so we can start matching right away. Prefer concrete, realistic job titles that exist in job postings.
- List every guess in "assumptions", phrased to the person ("We assumed you'd like …"), so they can correct us.
- Hard limits are different: deal_breakers, industries_excluded, salary and locations only from what the text says.
- Rate "clarity" honestly. If it is 3 or lower, ask 1-3 short, friendly, specific questions in "questions",
  addressed to the person, about what would improve their matches most (e.g. field, location, level).
- "intro" opens the first email we send. Show that we really read the text: pick up something specific from it and
  match its tone. If the text is playful, ironic or jokey, answer with light, kind humour of your own (for example
  playfully "considering" an absurd shortcut before asking for more details). If the text is serious, be warm and
  professional, no jokes. Write original wording for every person – no stock phrases. Never mock the person, never
  be sarcastic about their situation, and never suggest anything illegal, discriminatory, sexual or otherwise
  offensive. No names or personal details.
- NEVER copy personal data into the result: no names, emails, phone numbers, addresses, employer names the person
  may want to keep private, health or other sensitive details.
- If feedback on jobs we already sent is given, use it to refine the criteria: roles, seniority, fields, keywords and
  deal breakers (e.g. several 👎 "not my field" for QA roles → drop QA-like roles; 👍 on platform roles → prefer
  them). The person's own text stays the primary source; never contradict it because of a single vote.
- The text may be in any language. Write the fields addressed to the person (summary, intro, assumptions,
  questions) in the language named below the text; write every other field in English.
- Answer with a single JSON object that matches the schema, nothing else."""


def build_user_prompt(text: str, feedback: list[dict] | None = None, lang: str = "en") -> str:
    prompt = f'JSON schema:\n{SCHEMA}\n\nBrainstorm text:\n"""\n{text}\n"""'
    prompt += f"\n\nLanguage for summary, intro, assumptions and questions: {i18n.NAMES[i18n.pick(lang)]}"
    if feedback:
        votes = "\n".join(
            f"- {'👍' if f['vote'] == 'up' else '👎'} {f['title']} ({f['company']})"
            + (f" – {f['reasons']}" if f.get("reasons") else "")
            for f in feedback
        )
        prompt += f"\n\nFeedback on jobs we sent (most recent first):\n{votes}"
    return prompt


def validate(obj: dict) -> dict:
    """Coerce model output into the schema; raises ValueError if it is unusable."""
    try:
        profile = SearchProfile.model_validate(obj)
    except ValidationError as e:
        raise ValueError(f"schema validation failed: {e.error_count()} errors") from e
    # A vague text (no roles, no field) is still a valid result: the manage page asks the user to add details,
    # based on missing_info. Retrying the model would not make the text more concrete.
    return profile.model_dump()


def is_searchable(derived: dict) -> bool:
    return bool(derived.get("target_roles") or derived.get("fields"))


def needs_help(derived: dict | None) -> bool:
    """Should the next email ask the person to tell us more?"""
    return (
        bool(derived)
        and (derived.get("clarity", 3) <= 3 or not is_searchable(derived))
        and bool(derived.get("questions") or not is_searchable(derived))
    )
