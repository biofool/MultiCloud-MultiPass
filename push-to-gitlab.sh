#!/usr/bin/env bash
# Push six projects to GitLab under biofool-vig, following the existing
# convention from mirror-to-gitlab.sh (-gitlab suffix, private, token in
# push URL only, clean remote URL left behind).
#
# Pushes committed history only: --all --tags. No commits, no --mirror,
# no force. Non-fast-forward rejections are reported, not overridden,
# and cause a nonzero exit.
#
# Env: DRY_RUN=1  resolve namespace and check project existence, but skip
#                 the project create, the push, and the remote update.
set -uo pipefail

PROJECTS_ROOT="$HOME/projects"
ENV_FILE="$PROJECTS_ROOT/.env.secrets.gitlab"
DRY_RUN="${DRY_RUN:-0}"

# [fix 6] verify the secrets file exists before sourcing, so a missing file
# cannot silently fall through to a stale exported GITLAB_API.
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found." >&2
  exit 1
fi
set +u
# shellcheck disable=SC1090
source "$ENV_FILE"; set -u
if [[ -z "${GITLAB_API:-}" ]]; then
  echo "ERROR: GITLAB_API unset after sourcing $ENV_FILE" >&2
  exit 1
fi

GITLAB_URL="https://gitlab.com"
API="$GITLAB_URL/api/v4"
NAMESPACE="biofool-vig"

# [fix 7] pass values via argv rather than interpolating into a Python literal.
urlenc() { python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$1"; }

# local_path | gitlab_project_name
declare -a REPOS=(
  "AIRichardMoon|AIRichardMoon-gitlab"
  "WorldStudioFinder|WorldStudioFinder-gitlab"
  "AikiField.com|AikiField.com-gitlab"
  "quantumaikido.com|quantumaikido.com-gitlab"
  "MultiCloud-MultiPass|MultiCloud-MultiPass-gitlab"
  "VaultsshCA|VaultsshCA-gitlab"
)

failures=0
declare -a SKIPPED=()
declare -a OK=()

NAMESPACE_ID=$(curl -sS --fail -H "PRIVATE-TOKEN: $GITLAB_API" \
  "$API/namespaces/$NAMESPACE" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])") || {
  echo "ERROR: cannot resolve namespace $NAMESPACE" >&2; exit 1; }
echo "namespace $NAMESPACE -> id $NAMESPACE_ID"
if [[ "$DRY_RUN" == "1" ]]; then
  echo "*** DRY RUN — no projects will be created, nothing pushed, remotes untouched ***"
fi
echo

for entry in "${REPOS[@]}"; do
  local_path="${entry%%|*}"
  project="${entry##*|}"
  repo_dir="$PROJECTS_ROOT/$local_path"

  echo "============================================================"
  echo "$local_path  ->  $NAMESPACE/$project"
  echo "============================================================"

  # [fix 4] confirm the path is a real git work tree before doing anything.
  if ! git -C "$repo_dir" rev-parse --git-dir >/dev/null 2>&1; then
    echo "  ERROR: $repo_dir is not a git work tree — skipping" >&2
    SKIPPED+=("$local_path (not a git work tree)"); failures=$((failures+1)); continue
  fi

  enc=$(urlenc "$NAMESPACE/$project")
  code=$(curl -sS -o /dev/null -w "%{http_code}" \
    -H "PRIVATE-TOKEN: $GITLAB_API" "$API/projects/$enc")

  # [fix 1] only 404 means "create". Any other non-200 is an error, not a
  # reason to attempt a create that will fail and skip a pushable repo.
  if [[ "$code" == "200" ]]; then
    echo "  project exists"
  elif [[ "$code" == "404" ]]; then
    if [[ "$DRY_RUN" == "1" ]]; then
      echo "  [DRY_RUN] would create project $NAMESPACE/$project (private)"
    else
      echo "  creating project (private)..."
      if curl -sS --fail -H "PRIVATE-TOKEN: $GITLAB_API" -X POST "$API/projects" \
           -d "name=$project" -d "path=$project" \
           -d "namespace_id=$NAMESPACE_ID" -d "visibility=private" >/dev/null; then
        echo "  created"
      else
        # [fix 2] error to stderr, recorded, counted.
        echo "  ERROR: could not create $NAMESPACE/$project — skipping" >&2
        SKIPPED+=("$local_path (create failed)"); failures=$((failures+1)); continue
      fi
    fi
  else
    echo "  ERROR: unexpected HTTP $code checking $NAMESPACE/$project — skipping" >&2
    SKIPPED+=("$local_path (HTTP $code on existence check)"); failures=$((failures+1)); continue
  fi

  clean_url="$GITLAB_URL/$NAMESPACE/$project.git"
  authed_url="https://oauth2:${GITLAB_API}@gitlab.com/${NAMESPACE}/${project}.git"

  nbranches=$(git -C "$repo_dir" branch --format='%(refname:short)' | wc -l | tr -d ' ')
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [DRY_RUN] would push $nbranches branches + tags to $NAMESPACE/$project"
    OK+=("$local_path -> $NAMESPACE/$project (dry-run)")
  else
    echo "  pushing $nbranches branches + tags ..."

    git -C "$repo_dir" push "$authed_url" --all 2>&1 \
      | sed "s#$GITLAB_API#***#g" | sed 's/^/    /'
    all_rc=${PIPESTATUS[0]}

    git -C "$repo_dir" push "$authed_url" --tags 2>&1 \
      | sed "s#$GITLAB_API#***#g" | sed 's/^/    /'
    tag_rc=${PIPESTATUS[0]}

    # [fix 3] push failures must be surfaced and must affect the exit status.
    if (( all_rc != 0 || tag_rc != 0 )); then
      echo "  ERROR: push failed (branches rc=$all_rc tags rc=$tag_rc) — NOT force-pushing" >&2
      SKIPPED+=("$local_path (push rc=$all_rc/$tag_rc)"); failures=$((failures+1))
    else
      echo "  pushed OK ($nbranches branches + tags)"
      OK+=("$local_path -> $NAMESPACE/$project")
    fi
  fi

  # [fix 5] only claim the clean remote was set if it actually succeeded.
  # In dry-run we leave the remote untouched.
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [DRY_RUN] would set tokenless 'gitlab' remote -> $clean_url"
  else
    if git -C "$repo_dir" remote get-url gitlab >/dev/null 2>&1; then
      remote_cmd=(git -C "$repo_dir" remote set-url gitlab "$clean_url")
    else
      remote_cmd=(git -C "$repo_dir" remote add gitlab "$clean_url")
    fi
    if "${remote_cmd[@]}"; then
      echo "  gitlab remote -> $clean_url"
    else
      echo "  WARNING: could not set tokenless 'gitlab' remote in $repo_dir" >&2
      failures=$((failures+1))
    fi
  fi
  echo
done

echo "============================================================"
echo "Pushed OK (${#OK[@]}):"
for r in ${OK+"${OK[@]}"}; do echo "  - $r"; done
if (( ${#SKIPPED[@]} > 0 )); then
  echo "Problems (${#SKIPPED[@]}):"
  for r in "${SKIPPED[@]}"; do echo "  - $r"; done
fi
echo "============================================================"

(( failures == 0 )) || { echo "Completed with $failures failure(s)." >&2; exit 1; }
if [[ "$DRY_RUN" == "1" ]]; then
  echo "Dry run complete — no changes made."
else
  echo "All six pushed successfully."
fi
