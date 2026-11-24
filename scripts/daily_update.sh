#!/bin/bash
# Daily refresh: pull new signals, score them, match parcels, then republish the public snapshot.
# Run from anywhere: scripts/daily_update.sh   (expects a virtualenv at .venv and data/edradar.db in the repo folder)
set -euo pipefail
cd "$(dirname "$0")/.."

# One update at a time. Two at once (the nightly run and a hand-started republish) fight over the database, take hours,
# and whichever finishes last overwrites the other's snapshot. A lock left by a run that died is cleared after 12 hours.
LOCK=data/.update.lock
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +720 2>/dev/null)" ]; then rmdir "$LOCK" 2>/dev/null; mkdir "$LOCK"
  else echo "Another update is already running (lock at $LOCK). Not starting a second one." >&2; exit 75; fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT
EDRADAR=.venv/bin/edradar
DB=data/edradar.db
count() { [ -f "$DB" ] && sqlite3 "$DB" "select count(*) from $1" || echo 0; }

signals_before=$(count signals); leads_before=$(count leads); parcels_before=$(count parcels)

# EDRADAR_SKIP_RUN=1 republishes the site (a new blog post, a page edit) without collecting data first.
[ "${EDRADAR_SKIP_RUN:-0}" = "1" ] || "$EDRADAR" run --limit "${EDRADAR_MATCH_LIMIT:-1500}"
"$EDRADAR" export --out site

if [ "${EDRADAR_PUBLISH:-1}" = "1" ]; then
  remote=$(git remote get-url origin)
  # A large snapshot needs a bigger HTTP buffer or the push is rejected. Any failure here must fail the run.
  ( cd site && rm -rf .git && git init -q -b gh-pages && git config http.postBuffer 157286400 && git add -A \
    && git commit -q -m "Publish dashboard snapshot $(date +%F)" && git push -q -f "$remote" gh-pages ) \
    || { rm -rf site/.git; echo "Publishing the snapshot FAILED; the public page was not updated." >&2; exit 1; }
  rm -rf site/.git
  echo "Published snapshot to the gh-pages branch."
fi

echo "Daily update $(date '+%F %H:%M %Z'):"
echo "  new signals: $(( $(count signals) - signals_before ))"
echo "  new parcels: $(( $(count parcels) - parcels_before ))"
echo "  new leads:   $(( $(count leads) - leads_before ))  (total $(count leads))"
