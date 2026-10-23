# Decisions

## 2026-10-23 — GitLab usage excised — service no longer used

GitLab is no longer used across the portfolio: the `gitlab` git remotes
and the GitHub→GitLab mirrors are gone. `mirror-to-gitlab.sh`,
`push-to-gitlab.sh`, `gitlab-migrate.sh`, and
`.ai-context/components/gitlab-mirror.md` were already removed in 804a985;
this finishes the excision by dropping the stale `gitlab-mirror` references
from `.ai-context/index.md`, `components/index.md`, `manifest.yaml`,
`testing/test-map.yaml`, `change-impact/relationships.yaml`, and the
key-files lists in `wiki/Architecture.md` + `.devin/wiki/architecture.md`.
Historical mentions remain in `.ai-context/deltas/`, `decisions/conflicts.yaml`,
`debt/register.yaml`, and `TEST_PLAN.md` (title) by design.

## 2026-10-12 — adopted .devin/wiki layout

Seeded from the flat `wiki/` mini-wiki during the project-wiki migration. Append-only from here; every entry dated.

- 2026-10-12 — Adversarial review (magic-compact sweep); findings ticketed in GitHub issues.
