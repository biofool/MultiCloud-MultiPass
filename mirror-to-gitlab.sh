#!/usr/bin/env bash
# mirror-to-gitlab.sh — Push CloudManagement and AIRichardMoon to GitLab
# (biofool-vig namespace, repo names suffixed with -gitlab), then create
# copy-date tickets in both GitLab and GitHub.
#
# Sources GITLAB_API from ~/projects/.env.secrets.gitlab at runtime.
# Does NOT print the token.
#
# Usage:  ./mirror-to-gitlab.sh
set -euo pipefail

PROJECTS_ROOT="$HOME/projects"
ENV_FILE="$PROJECTS_ROOT/.env.secrets.gitlab"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found." >&2
  exit 1
fi

# Source the secrets file (sets GITLAB_API and possibly others).
# shellcheck disable=SC1090
set +u
source "$ENV_FILE"
set -u

if [[ -z "${GITLAB_API:-}" ]]; then
  echo "ERROR: GITLAB_API is not set after sourcing $ENV_FILE." >&2
  exit 1
fi

GITLAB_URL="https://gitlab.com"
GITLAB_API_BASE="$GITLAB_URL/api/v4"
NAMESPACE="biofool-vig"

# Pass values via argv rather than interpolating into a Python literal
# (same hardening as push-to-gitlab.sh "[fix 7]").
urlenc() { python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$1"; }

# Strip the token from any command output before it reaches the terminal or
# a log file (same redaction as push-to-gitlab.sh).
redact() { sed "s#${GITLAB_API}#***#g"; }

DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DATE_HUMAN="$(date -u +%Y-%m-%d)"

# Repos to mirror: local_dir  github_owner/repo  branches(space-sep)
declare -a REPOS=(
  "CloudManagement  biofool/CloudManagement  main dev"
  "AIRichardMoon    biofool/AIRichardMoon    main dev staging"
  "ChaosEngine      biofool/ChaosEngine      main"
)

echo "=== GitLab mirror script — $DATE_HUMAN ==="
echo

# --- Resolve GitLab user ID for the namespace ---
echo "Resolving GitLab namespace '$NAMESPACE'..."
NAMESPACE_RESP=$(curl -sS --fail \
  -H "PRIVATE-TOKEN: $GITLAB_API" \
  "$GITLAB_API_BASE/namespaces/$NAMESPACE")
NAMESPACE_ID=$(echo "$NAMESPACE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "  namespace id: $NAMESPACE_ID"
echo

# --- Helper: create a GitLab project if it doesn't exist ---
create_gitlab_project() {
  local project_name="$1"
  local encoded_path
  encoded_path=$(urlenc "$NAMESPACE/$project_name")

  # Check if it already exists
  local exists
  exists=$(curl -sS -o /dev/null -w "%{http_code}" \
    -H "PRIVATE-TOKEN: $GITLAB_API" \
    "$GITLAB_API_BASE/projects/$encoded_path")

  if [[ "$exists" == "200" ]]; then
    echo "  GitLab project $NAMESPACE/$project_name already exists."
    return 0
  fi

  echo "  Creating GitLab project $NAMESPACE/$project_name ..."
  curl -sS --fail \
    -H "PRIVATE-TOKEN: $GITLAB_API" \
    -X POST "$GITLAB_API_BASE/projects" \
    -d "name=$project_name" \
    -d "path=$project_name" \
    -d "namespace_id=$NAMESPACE_ID" \
    -d "visibility=private" \
    > /dev/null
  echo "  created."
}

# --- Helper: create a GitLab issue ---
create_gitlab_issue() {
  local project_name="$1"
  local title="$2"
  local body="$3"
  local encoded_path
  encoded_path=$(urlenc "$NAMESPACE/$project_name")

  curl -sS --fail \
    -H "PRIVATE-TOKEN: $GITLAB_API" \
    -X POST "$GITLAB_API_BASE/projects/$encoded_path/issues" \
    --data-urlencode "title=$title" \
    --data-urlencode "description=$body" \
    > /dev/null
  echo "  GitLab issue created in $NAMESPACE/$project_name"
}

# --- Helper: create a GitHub issue via gh ---
create_github_issue() {
  local gh_repo="$1"
  local title="$2"
  local body="$3"
  gh issue create --repo "$gh_repo" --title "$title" --body "$body" --label documentation 2>/dev/null || \
  gh issue create --repo "$gh_repo" --title "$title" --body "$body"
  echo "  GitHub issue created in $gh_repo"
}

# --- Process each repo ---
for entry in "${REPOS[@]}"; do
  read -r local_dir gh_repo branches <<< "$entry"
  repo_path="$PROJECTS_ROOT/$local_dir"
  gitlab_project="${local_dir}-gitlab"

  echo "============================================================"
  echo "Repo: $local_dir  (GitHub: $gh_repo  ->  GitLab: $NAMESPACE/$gitlab_project)"
  echo "Branches: $branches"
  echo "============================================================"

  # 1. Create GitLab project
  create_gitlab_project "$gitlab_project"

  # 2. Add/update gitlab remote
  gitlab_remote_url="$GITLAB_URL/$NAMESPACE/$gitlab_project.git"
  cd "$repo_path"
  if git remote get-url gitlab >/dev/null 2>&1; then
    git remote set-url gitlab "$gitlab_remote_url"
    echo "  Updated existing 'gitlab' remote -> $gitlab_remote_url"
  else
    git remote add gitlab "$gitlab_remote_url"
    echo "  Added 'gitlab' remote -> $gitlab_remote_url"
  fi

  # 3. Push each branch (use the token in the URL for auth, then restore clean URL)
  for branch in $branches; do
    # Check the branch exists locally
    if ! git rev-parse --verify "$branch" >/dev/null 2>&1; then
      echo "  SKIP branch '$branch' (does not exist locally)"
      continue
    fi
    echo "  Pushing $branch ..."
    # Push using the token via the remote URL with credentials embedded.
    # We temporarily set the URL with the token, push, then restore.
    authed_url="https://oauth2:${GITLAB_API}@gitlab.com/${NAMESPACE}/${gitlab_project}.git"
    git push "$authed_url" "$branch:refs/heads/$branch" --force-with-lease 2>&1 | redact | sed 's/^/    /'
    echo "  pushed $branch"
  done

  # 4. Restore clean remote URL (no embedded token)
  git remote set-url gitlab "$gitlab_remote_url"

  # 5. Create copy-date tickets
  issue_title="Mirror to GitLab — copy date $DATE_HUMAN"
  issue_body="This repository was mirrored to GitLab on **$DATE_HUMAN** (UTC: $DATE).

**GitLab destination:** [$NAMESPACE/$gitlab_project]($GITLAB_URL/$NAMESPACE/$gitlab_project)

**Branches mirrored:** $branches

**Source:** GitHub $gh_repo

This issue documents the copy date for audit purposes. The GitLab copy is a point-in-time snapshot and is not automatically kept in sync."

  echo "  Creating tickets..."
  create_gitlab_issue "$gitlab_project" "$issue_title" "$issue_body"
  create_github_issue "$gh_repo" "$issue_title" "$issue_body"

  echo
  cd "$PROJECTS_ROOT"
done

echo "============================================================"
echo "Done. All repos mirrored to GitLab and tickets created."
echo "Copy date: $DATE_HUMAN (UTC: $DATE)"
echo "============================================================"
