#!/usr/bin/env bash
set -euo pipefail

# Usage: ./migrate_repos.sh owner/repo1 owner/repo2 ...

GITLAB_GROUP="${GITLAB_GROUP:?Set GITLAB_GROUP to the target GitLab namespace/group}"

if (( $# == 0 )); then
  echo "Usage: $0 <owner/repo> [<owner/repo> ...]" >&2
  exit 1
fi

echo "== Logging in to GitLab =="
glab auth login

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

for repo in "$@"; do
  name="${repo##*/}"
  echo "== $repo -> ${GITLAB_GROUP}/${name} =="

  git clone --mirror "git@github.com:${repo}.git" "$workdir/$name.git"

  glab repo create "${GITLAB_GROUP}/${name}" --private -y \
    || echo "  (repo may already exist on GitLab, continuing)"

  git -C "$workdir/$name.git" push --mirror "git@gitlab.com:${GITLAB_GROUP}/${name}.git"

  rm -rf "$workdir/$name.git"
done

echo "Done."
