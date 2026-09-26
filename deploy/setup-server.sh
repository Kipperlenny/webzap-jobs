#!/usr/bin/env bash
# One-time server setup for WebZap Jobs. Run as your normal user: bash deploy/setup-server.sh
# Needs sudo. Steps: 1) GitHub CLI + login + publish repo  2) nginx site  3) Let's Encrypt certificate
# Usage: DOMAIN=example.com EMAIL=you@example.com REPO=you/webzap-jobs bash deploy/setup-server.sh
set -euo pipefail
cd "$(dirname "$0")/.."
DOMAIN=${DOMAIN:?set DOMAIN, e.g. DOMAIN=jobs.example.com}
EMAIL=${EMAIL:?set EMAIL (for Let's Encrypt)}
REPO=${REPO:?set REPO, e.g. REPO=you/webzap-jobs}

echo "== 1/3 GitHub CLI =="
if ! command -v gh >/dev/null; then
  sudo mkdir -p -m 755 /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
  sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
  sudo apt-get update -qq && sudo apt-get install -y gh
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "Log in to GitHub (choose: GitHub.com, HTTPS, login with a web browser):"
  gh auth login --hostname github.com --git-protocol https --web
fi
gh auth setup-git   # lets plain `git push/pull` over HTTPS use the gh login
if ! git remote get-url origin >/dev/null 2>&1; then
  if gh repo view "$REPO" >/dev/null 2>&1; then
    git remote add origin "https://github.com/$REPO.git" && git push -u origin main
  else
    gh repo create "$REPO" --public --source . --push \
      --description "Describe the job you want in your own words; get only matching open positions by email."
  fi
fi

echo "== 2/3 nginx site =="
sudo cp deploy/nginx-redact-tokens.conf /etc/nginx/conf.d/webzap-jobs-redact.conf
sed "s/example.com/$DOMAIN/g" deploy/nginx.conf.example | sudo tee "/etc/nginx/sites-available/$DOMAIN" >/dev/null
sudo ln -sf "/etc/nginx/sites-available/$DOMAIN" "/etc/nginx/sites-enabled/$DOMAIN"
sudo nginx -t && sudo systemctl reload nginx

echo "== 3/3 TLS certificate =="
ip=$(curl -s https://api.ipify.org)
if getent ahostsv4 "$DOMAIN" | grep -q "$ip"; then
  sudo certbot --nginx -d "$DOMAIN" -d "www.$DOMAIN" --redirect --non-interactive --agree-tos -m "$EMAIL"
else
  echo "!! $DOMAIN does not point to this server ($ip) yet. Set the A/AAAA records for $DOMAIN and www.$DOMAIN"
  echo "!! to $ip at netcup, wait for DNS, then run:"
  echo "   sudo certbot --nginx -d $DOMAIN -d www.$DOMAIN --redirect -m $EMAIL"
fi
echo "Done."
