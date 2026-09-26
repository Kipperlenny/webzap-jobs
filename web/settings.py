"""Configuration from environment variables (see .env.example)."""

import os
from pathlib import Path


def _req(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise RuntimeError(f"missing required environment variable {name}")
    return v


BASE_URL = os.environ.get("BASE_URL", "http://localhost:8010").rstrip("/")
DB_PATH = Path(os.environ.get("DB_PATH", "/data/signups.sqlite3"))
ENCRYPTION_KEY = _req("ENCRYPTION_KEY")  # Fernet key: encrypts email + text at rest
HASH_PEPPER = _req("HASH_PEPPER").encode()  # HMAC key for email lookup hashes
REPO_URL = os.environ.get("REPO_URL", "https://github.com/Kipperlenny/webzap-jobs")
CONTACT_EMAIL = _req("CONTACT_EMAIL")
GIT_COMMIT = os.environ.get("GIT_COMMIT", "dev")  # baked in at build time, shown in the footer
# SMTP settings (SMTP_* / BREVO_SMTP_*) are read by jobagent.mail.
MAIL_FROM = _req("MAIL_FROM")
# Imprint and privacy policy name the operator, so they are not part of the repository: each instance mounts its own
# <name>.<lang>.html files here (see README). LEGAL_BINDING_LANG: the version that is legally binding, e.g. "es".
LEGAL_DIR = Path(os.environ.get("LEGAL_DIR", "/legal"))
LEGAL_BINDING_LANG = os.environ.get("LEGAL_BINDING_LANG", "")

CONFIRM_TTL_HOURS = 48
MIN_TEXT = 20
MAX_TEXT = 5000
