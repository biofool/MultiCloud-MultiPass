# Component: GitLab Mirror Scripts

## Location
`mirror-to-gitlab.sh`, `push-to-gitlab.sh`, `gitlab-migrate.sh`

## Responsibility
Mirror GitHub repos to GitLab (`biofool-vig` namespace, `-gitlab` suffix).
Create GitLab projects, push branches + tags, create copy-date tickets in
GitLab and GitHub, and set tokenless `gitlab` remote.

## Scripts

| Script | Method | Scope |
|--------|--------|-------|
| `mirror-to-gitlab.sh` | `git push` per branch | CloudManagement, AIRichardMoon, ChaosEngine |
| `push-to-gitlab.sh` | `git push --all --tags` | 6 repos (quantumaikido.com, MultiCloud-MultiPass, etc.) |
| `gitlab-migrate.sh` | `git push --mirror` | Arbitrary `owner/repo` args |

## Dependencies
- `GITLAB_API` env var (sourced from `~/projects/.env.secrets.gitlab`)
- `curl` — GitLab API calls
- `gh` CLI — GitHub issue creation
- `git` — push operations
- `python3` — URL encoding helper

## Security
- Token never printed — `redact()` function strips `GITLAB_API` from output
- `push-to-gitlab.sh` uses `GIT_ASKPASS` (temp script) instead of URL-embedded token
- Tokenless `gitlab` remote set after push (clean URL, no credential stored)
- `DRY_RUN=1` supported by all scripts

## Security sensitivity
MEDIUM — handles GitLab API token. Token redaction is critical.

## Before modifying
- Never print `GITLAB_API` — always pipe through `redact()`
- `DRY_RUN=1` must work for all scripts
- Push failures must be surfaced (never fail silently — AGENTS.md rule)

## Test map target
No automated tests — manual execution with `DRY_RUN=1`
