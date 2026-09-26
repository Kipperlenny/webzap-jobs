"""Paths and secrets. Search settings live in precision profiles (see profiles/example.toml)."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "agent.sqlite3"
REPORT_DIR = DATA_DIR / "reports"
PRIVATE_PROFILES = ROOT / "private" / "profiles"  # gitignored: your own detailed searches
EXAMPLE_PROFILE = ROOT / "profiles" / "example.toml"

# Secrets: ./.env, plus an optional extra env file (JOBAGENT_ENV_FILE).
ENV_FILES = [ROOT / ".env"] + ([Path(os.environ["JOBAGENT_ENV_FILE"])] if os.environ.get("JOBAGENT_ENV_FILE") else [])
