#!/usr/bin/env bash
# One-shot GitHub setup for the watcher.
#
#   ./push_to_github.sh [repo-name]
#
# Creates the repo, pushes, grants the workflow write access, stores your
# notification secrets and kicks off a test run.
#
# Secrets are typed straight into `gh` and go to GitHub's encrypted store.
# They are never written to disk, never committed, and never printed.

set -euo pipefail

REPO="${1:-zernike-watch}"
cd "$(dirname "$0")"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }

# --- 0. preflight -----------------------------------------------------------
command -v gh >/dev/null || { echo "gh not installed:  brew install gh"; exit 1; }

if ! gh auth status >/dev/null 2>&1; then
  bold "You need to log in to GitHub first."
  echo
  echo "  Run:  gh auth login"
  echo
  echo "  Choose:  GitHub.com  →  HTTPS  →  Login with a web browser"
  echo "  Then re-run this script."
  exit 1
fi
ok "gh authenticated as $(gh api user --jq .login)"

# --- 1. create + push -------------------------------------------------------
bold "Creating repository..."
if git remote get-url origin >/dev/null 2>&1; then
  warn "remote 'origin' already exists ($(git remote get-url origin)) - pushing to it"
  git push -u origin main
else
  # Public on purpose: public repos get unlimited free Actions minutes.
  # A private repo caps at 2,000/month and this needs roughly 43,000.
  gh repo create "$REPO" --public --source=. --remote=origin --push \
    --description "Personal availability notifier for student housing in Groningen"
fi
SLUG=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
ok "pushed to $SLUG"

# --- 2. let the workflow commit its state file ------------------------------
bold "Granting the workflow write access..."
gh api -X PUT "repos/$SLUG/actions/permissions/workflow" \
  -f default_workflow_permissions=write \
  -F can_approve_pull_request_reviews=false >/dev/null
ok "workflow permissions set to read/write"

# --- 3. secrets -------------------------------------------------------------
bold "Notification secrets"
echo "  Press Enter to skip any you are not using."
echo "  Input is hidden and goes straight to GitHub's encrypted store."
echo

set_secret() {
  local name="$1" desc="$2" val
  printf '  %s (%s): ' "$name" "$desc"
  read -rs val; echo
  if [ -n "$val" ]; then
    printf '%s' "$val" | gh secret set "$name" --repo "$SLUG"
    ok "$name stored"
  else
    warn "$name skipped"
  fi
}

set_secret TELEGRAM_BOT_TOKEN "from @BotFather"
set_secret TELEGRAM_CHAT_ID   "from setup_telegram.py"
set_secret NTFY_TOPIC         "your ntfy topic"
set_secret DISCORD_WEBHOOK    "optional"

echo
bold "Secrets now set:"
gh secret list --repo "$SLUG" | sed 's/^/  /'

# --- 4. test run ------------------------------------------------------------
echo
bold "Triggering a test notification..."
gh workflow run watch.yml --repo "$SLUG" -f test_notify=true >/dev/null 2>&1 \
  && ok "test run queued" \
  || warn "could not trigger automatically - run it from the Actions tab"

echo
bold "Done."
echo
echo "  Repo:     https://github.com/$SLUG"
echo "  Actions:  https://github.com/$SLUG/actions"
echo
echo "  You should get a test notification within a minute or two."
echo "  After that it checks roughly every 90 seconds, forever."
echo
echo "  If no test notification arrives, open the Actions tab and read the"
echo "  run log - it prints exactly which channel failed and why."
