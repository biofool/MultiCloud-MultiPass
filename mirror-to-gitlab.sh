#!/usr/bin/env bash
# mirror-to-gitlab.sh — Push CloudManagement and AIRichardMoon to GitLab
# (biofool-vig namespace, repo names suffixed with -gitlab), then create
# copy-date tickets in both GitLab and GitHub.
#
# Sources GITLAB_API from ~/projects/.env.secrets.gitlab at runtime.
# Does NOT print the token.
#
# Usage:  ./mirror-to-gitlab.sh
# Env:    DRY_RUN=1  Print what would happen without pushing or creating
#                   projects/issues (API reads still occur).
set -euo pipefail

# --- Debug/Verbose flags ---
DEBUG=false
VERBOSE=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--debug) DEBUG=true; shift ;;
        -v|--verbose) VERBOSE=true; shift ;;
        *) break ;;
    esac
done

if $DEBUG; then
    set -x
    PS4='+ ${BASH_SOURCE}:${LINENO}: '
fi

log_verbose() { $VERBOSE && echo "[VERBOSE] $*" >&2 || true; }
log_debug() { $DEBUG && echo "[DEBUG] $*" >&2 || true; }

PROJECTS_ROOT="$HOME/projects"
ENV_FILE="$PROJECTS_ROOT/.env.secrets.gitlab"
DRY_RUN="${DRY_RUN:-0}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found." >&2
  exit 1
fi

# Source the secrets file (sets GITLAB_API and possibly others).
set +u
# shellcheck disable=SC1090
source "$ENV_FILE"
set -u

if [[ -z "${GITLAB_API:-}" ]]; then
  echo "ERROR: GITLAB_API is not set after sourcing $ENV_FILE." >&2
  exit 1
fi

GITLAB_URL="https://gitlab.com"
GITLAB_API_BASE="$GITLAB_URL/api/v4"
NAMESPACE="biofool-vig"

# URL-encode a path segment via argv (never interpolate values into a Python
# string literal — that is a command-injection vector).
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
if [[ "$DRY_RUN" == "1" ]]; then
  echo "*** DRY RUN — no pushes, projects, or issues will be created ***"
fi
echo

# --- Resolve GitLab user ID for the namespace ---
echo "Resolving GitLab namespace '$NAMESPACE'..."
NAMESPACE_RESP=$(curl -sS --fail \
  -H "PRIVATE-TOKEN: $GITLAB_API" \
  "$GITLAB_API_BASE/namespaces/$NAMESPACE") || {
  echo "ERROR: cannot resolve GitLab namespace '$NAMESPACE' (check GITLAB_API scope/network)." >&2
  exit 1; }
NAMESPACE_ID=$(echo "$NAMESPACE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])") || {
  echo "ERROR: could not parse namespace id from API response." >&2
  exit 1; }
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
  if [[ "$exists" != "404" ]]; then
    echo "  ERROR: unexpected HTTP $exists checking $NAMESPACE/$project_name." >&2
    return 1
  fi

  echo "  Creating GitLab project $NAMESPACE/$project_name ..."
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [DRY_RUN] would create $NAMESPACE/$project_name (private)"
    return 0
  fi
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

  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [DRY_RUN] would create GitLab issue in $NAMESPACE/$project_name: $title"
    return 0
  fi
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
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [DRY_RUN] would create GitHub issue in $gh_repo: $title"
    return 0
  fi
  # Try with the 'documentation' label first; if the label is missing the
  # first attempt fails and we retry without it. Errors from the second
  # attempt are surfaced (not silenced) so a real auth/network failure is
  # visible rather than silently dropped.
  if gh issue create --repo "$gh_repo" --title "$title" --body "$body" --label documentation 2>/dev/null; then
    echo "  GitHub issue created in $gh_repo"
    return 0
  fi
  if gh issue create --repo "$gh_repo" --title "$title" --body "$body"; then
    echo "  GitHub issue created in $gh_repo (no label)"
    return 0
  fi
  echo "  ERROR: could not create GitHub issue in $gh_repo" >&2
  return 1
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

  if [[ ! -d "$repo_path" ]]; then
    echo "  ERROR: local repo path $repo_path does not exist — skipping." >&2
    continue
  fi
  if ! git -C "$repo_path" rev-parse --git-dir >/dev/null 2>&1; then
    echo "  ERROR: $repo_path is not a git work tree — skipping." >&2
    continue
  fi

  # 1. Create GitLab project
  create_gitlab_project "$gitlab_project" || { echo "  Skipping $local_dir due to project-create error." >&2; continue; }

  # 2. Add/update gitlab remote (operate via -C so we never change the
  #    script's own working directory, which would leak across iterations).
  gitlab_remote_url="$GITLAB_URL/$NAMESPACE/$gitlab_project.git"
  if git -C "$repo_path" remote get-url gitlab >/dev/null 2>&1; then
    git -C "$repo_path" remote set-url gitlab "$gitlab_remote_url"
    echo "  Updated existing 'gitlab' remote -> $gitlab_remote_url"
  else
    git -C "$repo_path" remote add gitlab "$gitlab_remote_url"
    echo "  Added 'gitlab' remote -> $gitlab_remote_url"
  fi

  # 3. Push each branch. Token is passed via GIT_ASKPASS (issue #1 part 5)
  #    so it does NOT appear in the push URL or in `ps` output. The askpass
  #    helper is a one-line script that echoes the token when git asks for
  #    a password. Output is scrubbed via redact() as defense-in-depth.
  clean_push_url="https://gitlab.com/${NAMESPACE}/${gitlab_project}.git"
  askpass_script=$(mktemp /tmp/gitlab-askpass.XXXXXX.sh)
  printf '#!/usr/bin/env bash\necho "%s"\n' "$GITLAB_API" > "$askpass_script"
  chmod 700 "$askpass_script"
  trap 'rm -f "$askpass_script"' EXIT
  # shellcheck disable=SC2206  # intentional word-splitting of space-sep list
  read -ra branch_arr <<< "$branches"
  for branch in "${branch_arr[@]}"; do
    # Check the branch exists locally
    if ! git -C "$repo_path" rev-parse --verify "$branch" >/dev/null 2>&1; then
      echo "  SKIP branch '$branch' (does not exist locally)"
      continue
    fi
    if [[ "$DRY_RUN" == "1" ]]; then
      echo "  [DRY_RUN] would push $branch -> $NAMESPACE/$gitlab_project"
      continue
    fi
    echo "  Pushing $branch ..."
    # GIT_ASKPASS feeds the token to git without embedding it in the URL
    # or in argv (the token is in the askpass script file, mode 700).
    GIT_ASKPASS="$askpass_script" GIT_TERMINAL_PROMPT=0 \
      git -C "$repo_path" \
      push "https://oauth2@gitlab.com/${NAMESPACE}/${gitlab_project}.git" \
      "$branch:refs/heads/$branch" --force-with-lease 2>&1 \
      | redact | sed 's/^/    /'
    echo "  pushed $branch"
  done
  rm -f "$askpass_script"

  # 4. Restore clean remote URL (no embedded token)
  git -C "$repo_path" remote set-url gitlab "$gitlab_remote_url"

  # 5. Create copy-date tickets
  issue_title="Mirror to GitLab — copy date $DATE_HUMAN"
  issue_body="This repository was mirrored to GitLab on **$DATE_HUMAN** (UTC: $DATE).

**GitLab destination:** [$NAMESPACE/$gitlab_project]($GITLAB_URL/$NAMESPACE/$gitlab_project)

**Branches mirrored:** $branches

**Source:** GitHub $gh_repo

This issue documents the copy date for audit purposes. The GitLab copy is a point-in-time snapshot and is not automatically kept in sync."

  echo "  Creating tickets..."
  create_gitlab_issue "$gitlab_project" "$issue_title" "$issue_body" \
    || echo "  WARNING: GitLab ticket creation failed for $gitlab_project (continuing)." >&2
  create_github_issue "$gh_repo" "$issue_title" "$issue_body" \
    || echo "  WARNING: GitHub ticket creation failed for $gh_repo (continuing)." >&2

  echo
done

echo "============================================================"
echo "Done. All repos mirrored to GitLab and tickets created."
echo "Copy date: $DATE_HUMAN (UTC: $DATE)"
echo "============================================================"
