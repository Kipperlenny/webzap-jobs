# WebZap Jobs

Describe the job you want in your own words; get only matching open positions by email. Live at https://webzap.com.
Public repository (MIT), meant to be run by others too: never commit secrets, subscriber data or instance-specific
content (legal pages, personal profiles, server details) – those belong in `.env`, `private/` or `data/` (gitignored).
Settings that differ between instances go into `.env` / the TOML files, not into code.

## Layout

- `web/` – FastAPI + Jinja + SQLite sign-up app, runs in Docker (`web/Dockerfile`, `docker-compose.yml`).
  - `app.py` routes · `store.py` all DB access (encryption, hashed tokens, stats counters) · `settings.py` env config
  - `digest.py` daily subscriber digest · `matching.py` strict rule-based matching · `extract.py` + `worker.py`
    turn the free text into search fields via an LLM task queue · `external.py` links to sites we can't search
    (`external.toml`) · `golinks.py` `/go` click counter · `stats.py` operator numbers · `i18n.py` +
    `locales/*.toml` (en, de, es, fr)
  - The LLM runs once per subscriber (on confirmation, text/language change, or after 5 new 👍/👎 votes) to derive
    search fields; matching every posting is rule-based, no LLM. Partner feeds: `jobagent/feeds.py` (active when
    their keys are set).
- `jobagent/` – separate precision job search for personal TOML profiles (`python3 -m jobagent.run`), runs from cron
  on the host; the LLM assesses every posting that passes the rules (cached per job). Profiles live in
  `private/profiles/`. It learns instead of relying on fixed lists: `titles.py` (model accepts uncovered titles once
  per title), `companies.py` (similar companies from `[companies] like` → their job boards), `votes.py` (👍/👎 from
  the digest via signed links to the web app's `/feedback/`, pulled back from `/agent-feedback`) and `feedback.py`
  (the same votes on the command line).
- `digest.toml`, `external.toml` – sources for the digest; `profiles/example.toml` – profile template.
- `tests/` – pytest; `conftest.py` sets a throw-away env (temp DB, fake keys) before modules import.

## Commands

```
python3 -m venv .venv && .venv/bin/pip install -r web/requirements.txt pytest ruff httpx
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/pytest -q
```

CI (`.github/workflows/ci.yml`) runs ruff check, pytest and pip-audit. Line length 120.

## Principles (don't break these)

- Privacy is the product: no cookies, no trackers, no third-party requests, strict CSP (`script-src 'self'`,
  `form-action 'self'`), no request logging (uvicorn `--no-access-log`). Emails/texts encrypted at rest (Fernet),
  lookups via HMAC hash, tokens stored only as SHA-256.
- Ad measurement is only the anonymous `?c=<campaign>` counter per day (visits, sign-ups, confirmed). No gclid, no
  Google tag, no conversion pixels.
- State changes from email links go through a POST button page (mail scanners prefetch links).
- Every user-facing string exists in all four locales; a test checks identical keys and placeholders.
- Matching is strict on purpose: no match, no email.
- Legal pages are not in the repo (`private/legal/`, mounted as `/legal`).

## Careful

- The real `.env` contains live SMTP (Brevo) and partner API keys. Running `digest.py`, `jobagent.run` or the app's
  sign-up flow locally with it sends real email – use `--dry-run` or the test env.
- `data/` and `private/` are gitignored and hold real personal data.

## Instance notes

Operator-specific notes (server, deploy, ads) live outside the repository, if present:
@private/CLAUDE.md
