# Components Index

| Component | File(s) | Responsibility |
|-----------|---------|---------------|
| [Kill switch core](kill-switch-core.md) | `main.py`, `killswitch_actions.py` | Budget alert handler, kill orchestration |
| [Budget alert parser](budget-alert.md) | `alerts.py`, `dedup.py` | Pub/Sub parsing, threshold eval, dedup |
| [Quota poller](quota-poller.md) | `poller.py` | Real-time quota-spike detection |
| [Account registry](account-registry.md) | `registry.py` | Monitored account CRUD (Firestore/YAML) |
| [Intent/actual protocol](intent-actual.md) | `intent*.py` | Declare/report/kill lifecycle |
| [Provider adapters](providers.md) | `providers/` | Multi-cloud kill + billing fetch |
| [Dashboard](dashboard.md) | `dashboard.py`, `templates/` | Web UI + BigQuery billing API |
| [Inventory](inventory.md) | `inventory.py` | Unified resource inventory |
| [Admin routes](admin-routes.md) | `admin_routes.py` | Poll/reconcile/info endpoints |
| [Client package](client-package.md) | `cloud_management_client/` | Sub-project reporting client |
| [Path resolver](path-resolver.md) | `paths.py` | Project-root path resolution |
| [GitLab mirror](gitlab-mirror.md) | `mirror-to-gitlab.sh`, `push-to-gitlab.sh`, `gitlab-migrate.sh` | GitHub→GitLab mirroring |
