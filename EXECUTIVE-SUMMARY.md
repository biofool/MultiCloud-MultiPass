# Executive Summary — CloudManagement

CloudManagement is a multi-cloud cost kill switch and usage-reporting hub.
It helps small somatic studios and field teams control cloud spend and
operational risk when they run services such as booking systems, client
chat, video hosting, payment APIs, and automated outreach.

## What it does for a small studio

- **Prevents surprise cloud bills.** A runaway script, stuck cron job, or
  retry storm can rack up charges in hours. CloudManagement detects spikes
  and stops the runaway workload before the bill grows.
- **Protects client-facing services.** It can stop only the misbehaving job
  instead of taking down the whole project, so booking pages, client portals,
  and chat tools stay available.
- **Shows all cloud accounts in one place.** The inventory in
  `inventory.py` and the registry in `registry.py` list every monitored
  project, service, and job. A small team can see where data lives and
  where compute runs without logging into multiple consoles.
- **Helps use free tiers safely.** The intent/actual protocol lets each
  workload declare expected API calls before it runs. CloudManagement
  compares expected use against actual use, so the team can stay within
  free-tier limits and switch providers when a limit is near.

## How it controls cost and risk

- **Per-job kill:** Stops the single runaway job when actual API calls
  exceed declared intent.
- **Per-project kill:** Pauses billable services in a project when quota
  metrics spike.
- **Per-billing-account kill:** Unlinks billing as a last resort when a
  budget alert fires.

All actions are gated by an allowlist and a `DRY_RUN` mode, so operators
can test the system before it touches production workloads.

## Bottom line

For a somatic studio or field team that depends on cloud services but has
limited time to watch dashboards, CloudManagement provides automated guard
rails: it watches spend, isolates runaway costs, and keeps a single
inventory of cloud resources.
