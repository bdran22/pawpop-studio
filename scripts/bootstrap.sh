#!/usr/bin/env bash
# PAWPOP bootstrap — one command deployment to GitHub.
# Prereqs: git, curl, python3, pip; a classic PAT with repo+workflow scopes.
#
#   export GH_PAT="ghp_..." && bash scripts/bootstrap.sh
#
# Steps: create repo -> set Actions secrets -> push code -> upload EP004 seed
# assets as a release -> trigger the first run.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${GH_PAT:?export GH_PAT=<classic token with repo+workflow scopes>}"
REPO_NAME="${REPO_NAME:-pawpop-studio}"
# NOTE: the repo must be PUBLIC (default below): free Actions minutes are
# unlimited for public repos but capped at 2000 min/month for private ones,
# which cannot cover two ~3h production runs per day.
API="https://api.github.com"
AUTH=(-H "Authorization: Bearer $GH_PAT" -H "Accept: application/vnd.github+json" -H "User-Agent: pawpop-bootstrap")

echo "== 1. create repository =="
CREATE=$(curl -s -X POST "${AUTH[@]}" "$API/user/repos" \
  -d "{\"name\":\"$REPO_NAME\",\"private\":false,\"description\":\"PAWPOP STORIES autonomous production + 12h publishing agent\",\"auto_init\":false}")
FULL=$(echo "$CREATE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('full_name',''))")
if [ -z "$FULL" ]; then
  FULL=$(curl -s "${AUTH[@]}" "$API/user" | python3 -c "import json,sys; print(json.load(sys.stdin)['login'])")/$REPO_NAME
  echo "repo may already exist -> using $FULL"
fi
echo "repo: $FULL"

echo "== 2. push code =="
pip install --quiet pynacl 2>/dev/null || pip install --quiet --user pynacl
git init -q 2>/dev/null || true
git add -A
git -c user.name=pawpop-bot -c user.email=bot@pawpop commit -qm "PAWPOP autonomous pipeline: initial deploy" || true
git branch -M main
git remote remove origin 2>/dev/null || true
git remote add origin "https://x-access-token:${GH_PAT}@github.com/${FULL}.git"
git push -q -u origin main

echo "== 3. Actions secrets =="
GH_TOKEN="$GH_PAT" python3 scripts/gh_secrets.py "$FULL" "$GH_PAT"

echo "== 4. seed assets release (EP004 completed clips + VO) =="
SEED_TAR="/tmp/ep004_seed.tar.gz"
if [ -f "$SEED_TAR" ]; then
  SEED_SIZE=$(stat -c%s "$SEED_TAR")
  UPLOAD_URL=$(curl -s -X POST "${AUTH[@]}" "$API/repos/$FULL/releases" \
    -d '{"tag_name":"seed-assets","name":"seed-assets","body":"EP004 completed clips + VO (bootstrap)"}' | \
    python3 -c "import json,sys; print(json.load(sys.stdin).get('upload_url','').split('{')[0])")
  curl -s -X POST -H "Authorization: Bearer $GH_PAT" -H "Content-Type: application/gzip" \
    --data-binary @"$SEED_TAR" "${UPLOAD_URL}?name=ep004_seed.tar.gz" >/dev/null && \
    echo "seed uploaded ($SEED_SIZE bytes)"
fi

echo "== 5. trigger first run =="
curl -s -X POST "${AUTH[@]}" "$API/repos/$FULL/actions/workflows/pipeline.yml/dispatches" \
  -d '{"ref":"main","inputs":{"produce_budget_min":"265"}}' && echo "dispatched"

echo
echo "DEPLOYED: https://github.com/$FULL"
echo "Actions:  https://github.com/$FULL/actions"
