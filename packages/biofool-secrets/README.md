# biofool-secrets

Canonical secret resolution for the biofool portfolio — one implementation
of the `env → GCP Secret Manager → Proton Pass` chain and one `KEY_ALIASES`
registry, replacing 5 diverged copies (CloudManagement#129).

## Install

Pinned git dependency where the build can reach private GitHub:

```bash
pip install "git+ssh://git@github.com/biofool/biofool-secrets.git@<tag-or-sha>"
```

Consumer CI and Cloud Build (Kaniko) cannot authenticate to private
git, so those repos carry a `packages/biofool-secrets/` vendored snapshot
with a `VENDORED-FROM.txt` stamp naming source + sha. Sync snapshots
from this repo; do not edit vendored copies in place.

## Use

```python
from biofool_secrets import resolve_secret, list_secret_names, resolution_report

key = resolve_secret("GEMINI_API_KEY")                          # chain resolution
key = resolve_secret("GEMINI_API_KEY", project="story_graph")   # + project aliases
names = list_secret_names()                                     # names only, never values
report = resolution_report("GOOGLE_API_KEY")                    # names-only diagnostic
```

CLI:

```bash
python -m biofool_secrets resolve BRAVE_SEARCH_API_KEY --mask
python -m biofool_secrets list
python -m biofool_secrets env GEMINI_API_KEY BRAVE_SEARCH_API_KEY --dry-run
python -m biofool_secrets check GOOGLE_API_KEY
```

## Resolution chain

1. Deprecation canonicalization (registry file, structured WARNING once)
2. Environment variables — all candidates (canonical + aliases) first
3. GCP Secret Manager — `google-cloud-secretmanager` SDK if installed,
   else `gcloud` CLI (TTL + negative cached, per candidate)
4. Proton Pass (`pass-cli`) — only when `PROTON_PASS_ENABLED` + agent token
5. `default`

## Configuration (env vars)

| Var | Meaning |
|---|---|
| `SECRETS_BACKEND` | `auto` (default) / `env` / `sdk` / `gcloud`. `env` = kill-switch, never touches remote sources (CI/tests). |
| `USE_SECRET_MANAGER` | Back-compat: `false` → `env`, `true` → `auto` (only when `SECRETS_BACKEND` unset). |
| `SECRETS_PREFER_REMOTE` | `true` puts GSM *before* env (hub-side rotate-under-env semantics). Default: env-first. |
| `GCP_SECRET_PROJECT` / `SECRET_PROJECT_ID` / `GCP_PROJECT_ID` / `GOOGLE_CLOUD_PROJECT` / `PROJECT_ID` | GSM project, first set wins. Never hardcoded. |
| `SECRET_LOADER_CACHE_TTL` / `SECRET_LOADER_NEG_CACHE_TTL` | Positive / negative cache seconds (300 / 30). |
| `BIOFOOL_SECRETS_PROJECT` | Selects the `project_aliases` section of the registry. |
| `BIOFOOL_SECRETS_ALIASES_FILE` | JSON file merged over the bundled registry (its keys win). |
| `BIOFOOL_SECRETS_DEPRECATED_FILE` | Deprecated-alias YAML (else `config/deprecated_secrets.yaml` under cwd). |
| `PROTON_PASS_ENABLED` / `PROTON_PASS_AGENT_TOKEN` / `PROTON_PASS_CLI_PATH` / `PROTON_PASS_VAULT` / `PROTON_PASS_CACHE_TTL` | Proton Pass fallback (opt-in). |

## Prefixed Cloudflare tokens

Per-project tokens follow CloudManagement#154: `<PREFIX>_CFT`, then the tracked
duplicate `<PREFIX>_CFT_dup` (lowercase `_dup`), then `CLOUDFLARE_API_TOKEN`, then `CFT`.

```python
from biofool_secrets import resolve_token

name, value = resolve_token("AIKI", env_file="../.env.secrets")  # or None
os.environ["CLOUDFLARE_API_TOKEN"] = value  # wrangler reads only this name
```

Sources are tried in order: process env, `env_file` (optional), Secret Manager
(skipped under `SECRETS_BACKEND=env`). Inside each source the first name wins.
Values lose whitespace, CR and one pair of quotes; an empty value counts as unset.
Env files accept `export NAME=value`, skip comments, and a later duplicate never
replaces an earlier non-empty value. Log the returned NAME, never the value.

```bash
TOKEN=$(biofool-secrets token AIKI --env-file ../.env.secrets)   # winning name on stderr
```

## KEY_ALIASES registry

`biofool_secrets/key_aliases.json` — merged from the four diverged copies
(AIRichardMoon, WorldStudioFinder, VigilentSupport, story_graph):

- `aliases` — same-credential alternate spellings, safe for every repo
  (BRAVE_* family, JEV/OPENROUTER, R2/Cloudflare/AWS spellings, GCP project
  id spellings, OPENCAGE spellings).
- `project_aliases` — per-repo entries, applied only when the consumer
  selects itself (`BIOFOOL_SECRETS_PROJECT` or `project=`): AIR lineage
  `coaching-*` GSM ids, WSF `HOBBY_LOBBY_*`/`MOVEMENT_ARTS_*` names,
  story_graph's Google-key disambiguation.
- `notes` — documented hazards (e.g. `GOOGLE_API_KEY` must not be a global
  alias for Knowledge-Graph/Geocoding; `CLOUDFLARE_EMAIL_API_TOKEN` is
  deliberately *not* aliased to `CLOUDFLARE_API_TOKEN`).

## Lineage replaced (status)

| Copy | Status |
|---|---|
| `CloudManagement/secret_loader.py` | migrated — thin shim over `resolve_secret`/`fetch_secret` (opt-in `USE_SECRET_MANAGER` gate preserved) |
| `story_graph/config/secrets.py` | migrated — shim with `project="story_graph"` alias scope; `secret_project()` keeps its repo default |
| `OSenseiDocuments/osensei_archiver/secret_loader.py` | migrated — shim + vendored snapshot |
| `AIRichardMoon` / `WorldStudioFinder` / `VigilentSupport` `bootstrap_env.py` | migrated — fetch/alias/push machinery delegated (`gsm.push_secret`); repo workflows (templates, `--init`, OCI PEM) kept |
| `MultiCloud-MultiPass/secret_loader.py` | deferred — its only call is `GLOBAL_REPORT_TOKEN`, removed by the P5 convergence |

## Provenance

Split out of `biofool/CloudManagement` (`packages/biofool-secrets/`) via
`git subtree split` — history preserved. **This repo is the canonical
source.** CloudManagement's own `packages/biofool-secrets/` copy is now a
vendored snapshot like every other consumer's.
