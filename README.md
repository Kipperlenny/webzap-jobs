# WebZap Jobs

[![CI](https://github.com/Kipperlenny/webzap-jobs/actions/workflows/ci.yml/badge.svg)](https://github.com/Kipperlenny/webzap-jobs/actions/workflows/ci.yml)

Describe the job you want in your own words, and get only the matching open positions by email.

Live sign-up: **https://webzap.com**

There are two parts:

- **`web/`**: a small sign-up web app. You enter your email and write freely about the job you want: your field,
  interests, last position, location, and what you don't want. Sign-up uses a double opt-in (email confirmation link).
  Every email contains a link to delete your data. The app sets no cookies and uses no trackers.
- **`jobagent/`**: a daily precision job search (Python, runs from cron). It reads thousands of postings and judges
  them with a language model against a detailed personal profile. The digest contains only the few that really fit.

## Job agent: precision profiles ("your personal search")

The job agent is for people who want **few, precisely matching results** rather than many. Each search is one TOML file,
a *precision profile*, and each file can go to a different email address. A profile contains:
- a free-text candidate description;
- title, location and source settings;
- hard exclusion patterns and plain-language rules;
- compensation targets (salary and day rate);
- a weighted scoring rubric with penalties;
- caps on the number of results.

```
cp profiles/example.toml private/profiles/me.toml   # private/ is gitignored – add as many profiles as you like
python3 -m jobagent.run --dry-run                    # all profiles in private/profiles/, print instead of sending
python3 -m jobagent.run private/profiles/me.toml     # one profile, send the digest to its `email`
```

What happens on each run:

1. **Fetch once.** Every source that any profile lists is fetched a single time:
   - employer career boards via the public Greenhouse, Lever, Ashby, SmartRecruiters, Personio and Recruitee APIs;
   - the aggregators Remote OK, Arbeitnow, Himalayas and Jobicy.
   Copies of the same opening are then merged (see *Duplicates and caching* below).

2. **Rules** (for each profile). A posting must pass all of these:
   - the title matches;
   - the location is compatible;
   - it is recent enough (live postings on the employer's own career system always count as recent);
   - no stated salary is below the floor;
   - none of the hard exclusion patterns match the description (e.g. "fluent Spanish required").

   Aggregator links are checked to confirm the job is still open.
3. **Assessment by a language model**, through the connectors in `.env` (e.g. a self-hosted LM Studio). The model:
   - scores each rubric category and applies the penalties and exclusion rules;
   - extracts the salary with a confidence label (stated / estimated / unknown), plus the office, travel and language
     requirements;
   - writes "why it fits", "concerns", "flags" and "verify before applying".

   The code adds up the points, so the totals follow your rubric. A job that has been assessed once is not assessed
   again – not even when it turns up later as another copy – until you change the profile's candidate text, rules,
   compensation or rubric. If the model server is offline, the run waits for it (`--wait-hours`).
4. **Digest email.** It has up to N strong matches, N possible matches and N consulting/interim matches. If nothing
   fits, it says so plainly and adds a short list of newly rejected jobs, so near-misses aren't rediscovered. Each job
   is emailed once. An HTML copy of each digest is saved in `data/reports/`.
5. **External links.** Sites that fit the search but can't be searched automatically go into the profile as
   `[[external]]` (name, https URL, note). The digest lists up to `max_externals` of them in a clearly marked section,
   each at most once every `external_repeat_days` (default 30) – a reminder, not a newsletter.

Secrets live in `.env`:
- `LLM_*` (connectors);
- `SMTP_*` and/or `BREVO_SMTP_*`;
- `JOBAGENT_SENDER` and `JOBAGENT_REPLY_TO`.

Respect the job sources' API terms. Remote OK, Himalayas and Jobicy require a link back and credit (the digest does
both). Remotive's free API must not be used for services that collect sign-ups or email addresses.

Cron example: `30 7 * * * cd /path/to/webzap-jobs && python3 -m jobagent.run --wait-hours 8 >> data/run.log 2>&1`

## Web app

FastAPI, Jinja templates and SQLite, running in Docker.

```
cp .env.example .env      # fill in keys and SMTP
docker compose up -d --build
```

How it stores data:

- Email addresses and texts are **encrypted at rest** (Fernet).
- Email addresses are looked up via an HMAC hash.
- Confirmation and deletion tokens are stored only as SHA-256 hashes.
- Unconfirmed sign-ups are deleted after 48 h.

How it handles requests:

- The confirm and delete links in emails open a page with a button, and the button sends a POST. This stops email
  scanners from triggering either action just by following the link.
- There is a honeypot field and a rate limit per IP.
- It sends strict CSP and security headers, and loads nothing from third parties.
- The container runs read-only, as a non-root user, with all capabilities dropped.

Server setup (GitHub CLI, nginx, Let's Encrypt): `DOMAIN=example.com EMAIL=you@example.com bash deploy/setup-server.sh`

### Languages

The site and all emails exist in English, Spanish, French and German (`web/i18n.py`, strings in `web/locales/*.toml`;
a test checks that every locale has the same keys and placeholders).

- Pages take their language from the URL (`/es/`, `/fr/`, `/de/`, handled by one middleware, so routes stay
  language-free) or, without a prefix, from the browser's `Accept-Language`. No cookies.
- The language of the sign-up page is stored with the entry; emails and their links use it. People can change it on
  their manage page. The language model writes its summary and questions in that language, too.
- Legal pages: see below.

### Legal pages (imprint, privacy policy)

They name the operator, so **the repository ships none** – a copy of this code must not show someone else's imprint.
Each instance puts its own pages into `private/legal/` (gitignored, mounted read-only as `/legal`):
`privacy.<lang>.html`, `imprint.<lang>.html` and any partials they include. They extend `legal/_layout.html`:

```
{% extends "legal/_layout.html" %}
{% block heading %}Privacy policy{% endblock %}
{% block legal %}<p>…</p>{% endblock %}
```

A missing language falls back to `LEGAL_BINDING_LANG`, then English; with no page at all, the site shows a neutral
"not published yet" notice and the app logs a warning at start-up. Set `LEGAL_BINDING_LANG` (e.g. `es`) when one
version is legally binding: the other languages then say they are translations and link to it.

### Ad campaigns without tracking

Put `?c=<name>` on ad links, e.g. `https://example.com/de/?c=gads-de-alerts` (lowercase letters, digits, `-`, `_`).
The server adds up visits, sign-ups and confirmations per campaign and day – plain numbers, no cookies, no pixels, no
click IDs. The campaign name travels with the form and stays on an unconfirmed entry only until it is confirmed.
Turn off auto-tagging (gclid) in Google Ads; it is ignored anyway. Read the numbers with
`docker exec webzap-jobs-web python stats.py`.

## Subscriber digest

`web/digest.py` runs daily (cron: `docker exec webzap-jobs-worker python digest.py`, test with `--dry-run`).
- **Job pool:** the boards and aggregators in `digest.toml`, fetched once per run. On top of that, searches on the
  partner feeds (Adzuna, Jooble, Careerjet) are built from each subscriber's derived queries and location. Each feed is
  active only when its API key is set.
- **Matching** (`web/matching.py`): rule-based, fast, fully on the server, and **strict on purpose**. The idea is
  that one excellent job beats ten "maybe"s, because people stop reading emails full of the latter. A job is sent only
  if all of these hold:
  - the title really matches a wanted role, including its key word;
  - the location or remote region fits;
  - the posting mentions at least one of the person's topical keywords or skills;
  - no deal breaker applies;
  - it is a real opening (not a talent pool);
  - it scores at least 70.

  A digest has at most 3 jobs (daily) or 7 (weekly), and at most 2 per company. **If nothing is good enough, no email
  is sent.**
- **Email:**
  - daily subscribers get up to 5 new jobs every day, weekly subscribers up to 10 on Mondays; the first digest goes
    out right after confirmation;
  - right after confirming, a welcome email explains what happens next (only real matches, no mass mailings) and
    repeats the manage link;
  - the first mailing starts with a "help us understand you" box when the text was vague;
  - every job gets 👍/👎 links; 👎 asks for a reason, and "not this company" blocks that company;
  - manage link, one-click unsubscribe (RFC 8058 headers);
  - jobs are never sent twice.
- **External links** (`external.toml`, `web/external.py`): big job sites we can't search automatically (no open
  interface for services like ours) aren't left out silently. A digest may end with up to two of them, labelled
  "External link", with the person's search already filled in (their role, and their place where the site supports
  it). Only sites that fit the person are offered – their country, field, kind of work and a language they speak –
  each link at most once a month, and only in an email we send anyway: never an email just for these links. We store
  only a hash of each link shown (it contains their search).
- **Transparency:** partner links are labelled "Partner link · <name>". Sponsored jobs (`sponsored.example.toml`,
  real campaigns in `private/sponsored/`) are labelled "Sponsored". Sponsoring is a one-way street: sponsors get
  aggregate numbers only, never subscriber data.

## Duplicates and caching

The same opening appears on the employer's board and on several aggregators, often in slightly different forms:
"ibm" vs "International Business Machines", "Acme GmbH" vs "ACME", a title cut off at 80 characters, "(m/w/d)",
"- 100% remote", "Entwickler:in". `jobagent/dedup.py` merges these copies before any matching or model call. It is used by
both the job agent and the subscriber digest.

- **Merging:** copies merge when the normalised company and title are the same. Company names are compared without
  legal forms, as initials ("ibm") or with generic extras ("… Labs", "… Agency"). Titles are compared without gender
  tags, work mode, percentages and time zones. Places are kept, so "Engineer – Spain" and "Engineer – Germany" stay
  two jobs. Fuzzier cases join only copies from different sources whose descriptions agree (MinHash over word
  5-grams): similar titles, uncertain company names, and titles cut off by an aggregator. One board never lists a job
  twice, and companies reuse description boilerplate.
- **What is kept:** the employer's own posting wins, then the fullest description. Missing salary or employment type
  is filled in from the other copies.
- **Stable ids across runs:** a SQLite table (`job_alias`) maps every copy's key, and the normalised company and
  title, to one id. Stored assessments and "already sent" survive when the copy we saw first disappears or a copy
  turns up later under another name. Keys that existed before the table keep their meaning. Entries unseen for 90
  days are dropped.
- **Scale:** merging uses dictionary lookups; fuzzy checks only run within one title or one company (about 1 s for
  17,000 postings). In the digest, each subscriber is scored only against jobs whose title contains the key word of
  one of their roles (`matching.TitleIndex`, exact by construction). Partner-feed searches are normalised, so
  subscribers with the same search share one request, and they run in parallel. Link checks are cached per run.

## Text analysis: connectors and queue

After sign-up confirmation (and after each text edit), a task is queued to turn the free text into structured search
fields: roles, seniority, field, skills, locations, work mode, remote regions, employment type, salary floor, languages,
deal breakers, search queries and more (`web/extract.py`). The result is stored encrypted and shown on the user's manage
page.

- **Connectors** (`web/connectors.py`): any OpenAI-compatible server (LM Studio, Ollama, vLLM, OpenAI). They are
  configured only through environment variables, so no endpoints or keys live in the code. `LLM_CONNECTORS` lists them
  in order of preference. The reference setup uses a self-hosted LM Studio behind a tunnel. Protect it with an API
  token (`LLM_<NAME>_API_KEY`) and/or Cloudflare Access headers (`LLM_<NAME>_HEADERS`). If you use an external
  provider, user texts are sent to it, so update the privacy policy and get consent first.
- **Queue and worker** (`web/worker.py`, the `worker` service): tasks are stored in SQLite. When no connector is online,
  tasks wait. The worker checks again after 30 s, then after longer and longer pauses (up to 30 min), and offline time
  never counts as a failed attempt. Unusable model answers are retried up to 5 times. Tasks that crash mid-run are
  picked up again after a restart.

## Development

```
pip install -r web/requirements.txt pytest ruff httpx
ruff check . && ruff format --check . && pytest
```

CI runs lint, the tests and a dependency audit on every push (`.github/workflows/ci.yml`). Dependabot keeps
dependencies current. Security reports: see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE) © 2026 [Lennart Schreiber](https://github.com/Kipperlenny)
