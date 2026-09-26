#!/usr/bin/env bash
# Rebuild and restart the web app. The deployed commit is baked into the image and shown in the site footer,
# so visitors can check exactly which code is running. Refuses to deploy uncommitted or unpushed changes.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -n "$(git status --porcelain -- web jobagent digest.toml external.toml docker-compose.yml)" ]; then
  echo "web/, jobagent/ or config have uncommitted changes – commit and push first, so the footer commit matches GitHub." >&2; exit 1
fi
git fetch -q origin && [ -z "$(git log origin/main..HEAD --oneline)" ] || { echo "push to GitHub first" >&2; exit 1; }
GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
