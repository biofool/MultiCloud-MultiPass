#!/usr/bin/env bash
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

# Mirror GitHub repos into a GitLab group via `git push --mirror`.
#
# Usage: ./gitlab-migrate.sh owner/repo1 owner/repo2 ...
# Env:   GITLAB_GROUP  target GitLab namespace/group (required)
#        DRY_RUN=1     print what would happen; clone locally but skip the
#                      GitLab project create and the mirror push.
#        GLAB_HOST     glab hostname (default: gitlab.com)

GITLAB_GROUP="${GITLAB_GROUP:?Set GITLAB_GROUP to the target GitLab namespace/group}"
DRY_RUN="${DRY_RUN:-0}"
GLAB_HOST="${GLAB_HOST:-gitlab.com}"

if (( $# == 0 )); then
  echo "Usage: $0 <owner/repo> [<owner/repo> ...]" >&2
  exit 1
fi

# Validate each argument looks like owner/repo (no slashes elsewhere, no
# shell metacharacters that could be smuggled into a git URL).
for repo in "$@"; do
  if [[ ! "$repo" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]]; then
    echo "ERROR: '$repo' is not a valid owner/repo (expected owner/repo, alphanumerics/._- only)." >&2
    exit 1
  fi
done

echo "== Logging in to GitLab =="
if [[ "$DRY_RUN" == "1" ]]; then
  echo "[DRY_RUN] skipping glab auth login"
else
  glab auth login
fi

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

failures=0
for repo in "$@"; do
  name="${repo##*/}"
  echo "== $repo -> ${GITLAB_GROUP}/${name} =="

  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [DRY_RUN] would clone --mirror git@github.com:${repo}.git"
    echo "  [DRY_RUN] would create ${GITLAB_GROUP}/${name} (private)"
    echo "  [DRY_RUN] would push --mirror to git@${GLAB_HOST}:${GITLAB_GROUP}/${name}.git"
    continue
  fi

  git clone --mirror "git@github.com:${repo}.git" "$workdir/$name.git"

  # Create the GitLab project. A failure because the project already exists
  # is expected and non-fatal; any other failure is recorded so it is not
  # silently swallowed (AGENTS.md: never fail silently).
  if glab repo create "${GITLAB_GROUP}/${name}" --private -y 2>"$workdir/$name.create.err"; then
    echo "  created GitLab project ${GITLAB_GROUP}/${name}"
  else
    rc=$?
    if grep -qi "already exists\|409\|400.*already" "$workdir/$name.create.err" 2>/dev/null; then
      echo "  (repo may already exist on GitLab, continuing)"
    else
      echo "  ERROR: glab repo create failed (rc=$rc) for ${GITLAB_GROUP}/${name}:" >&2
      sed 's/^/    /' "$workdir/$name.create.err" >&2
      failures=$((failures+1))
      rm -rf "$workdir/$name.git"
      continue
    fi
  fi

  git -C "$workdir/$name.git" push --mirror "git@${GLAB_HOST}:${GITLAB_GROUP}/${name}.git"

  rm -rf "$workdir/$name.git"
done

echo "Done."
(( failures == 0 )) || { echo "Completed with $failures failure(s)." >&2; exit 1; }
