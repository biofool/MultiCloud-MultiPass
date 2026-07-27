# Per-Repo API Specifications for CloudManagement Intent/Actual Protocol

This document specifies the **exact** intent declarations, actual reports, kill descriptors, and expected-costs responses for each sub-project, grounded in the actual code call sites discovered by examining each repository.

The protocol endpoints are defined in `intent.py`:
- `POST /api/v1/intent` — declare expected usage before API calls
- `POST /api/v1/actual` — report actual usage (post-call or incremental)
- `GET /api/v1/expected-costs/<project_id>` — pull authoritative expected costs
- `POST /api/v1/kill/<intent_id>` — manual kill (dashboard)

All requests require `Authorization: Bearer <CLOUDMANAGEMENT_REPORT_TOKEN>` (per-project token).

---

## 1. AIRichardMoon (`your-org/AIRichardMoon`)

**CloudManagement project_id:** `your-project-1`
**GCP project:** `your-hub-project`
**Ticket:** https://github.com/your-org/AIRichardMoon/issues/44

### 1.1 Paid API calls (from code analysis)

| # | Provider | API | Call site | Cost model |
|---|----------|-----|-----------|------------|
| 1 | `gemini` | `models.generate_content` | `backend/app/clients.py:289` (`GeminiGenerator.generate()`) | Token-priced: $0.10/1M input, $0.40/1M output |
| 2 | `twilio` | `Messages.json` | `backend/functions/webhook_dispatcher/main.py:98` (`send_sms()`) | Per-SMS (only if `TWILIO_ENABLED=true`) |

**Not reported (free):** Firestore, Pub/Sub, OAuth (Google/Facebook/GitHub/Microsoft/Apple/Amazon), Cloudflare Turnstile, SMTP, Jitsi. These are tracked by the existing `ProviderCostTracker` for volume monitoring but have no per-call cost.

### 1.2 Gemini intent declaration

**When:** Before each `GeminiGenerator.generate()` call in `backend/app/service.py:generate_response()`.

**job_id:** The coaching `session_id` (Firestore session document ID). Multiple Gemini calls within one session share the same `job_id` — the intent declares the *session's* total expected usage, and actuals are reported incrementally per call.

```json
POST /api/v1/intent
Authorization: Bearer <token>
{
  "project_id": "your-project-1",
  "source_repo": "your-org/AIRichardMoon",
  "job_id": "<session_id>",
  "job_name": "coaching-session",
  "provider": "gemini",
  "api": "models.generate_content",
  "expected_calls": 1,
  "expected_cost_usd": 0.001,
  "expected_tokens": 4000,
  "rate_limit_rpm": 60,
  "window_start": "2026-07-24T18:00:00Z",
  "window_end": "2026-07-24T18:05:00Z",
  "kill": {
    "type": "http_callback",
    "url": "https://<cloud-run-url>/v1/admin/kill-session",
    "method": "POST",
    "headers": {
      "Authorization": "Bearer <DASHBOARD_ADMIN_KEY>",
      "Content-Type": "application/json"
    }
  },
  "metadata": {
    "model": "gemini-3.1-flash-lite",
    "temperature": 0.4,
    "max_output_tokens": 2048,
    "user_id": "<auth user id>"
  }
}
```

**Expected cost computation** (from `backend/app/costs.py:190`):
```
expected_cost_usd = (expected_input_tokens * 0.10 + expected_output_tokens * 0.40) / 1_000_000
```
Where `expected_input_tokens` ≈ len(context) + len(history) + len(message) in tokens (use the existing `GeminiCostTracker.estimate()` with a pre-call token estimate), and `expected_output_tokens` = `gemini_max_output_tokens` (2048).

**Response:**
```json
{
  "intent_id": "int_a1b2c3d4e5f6",
  "approved": true,
  "budget_remaining_usd": 8.50,
  "kill_switch_armed": true,
  "warnings": []
}
```

If `approved: false`, the coaching session should fall back to a canned response and not call Gemini.

### 1.3 Gemini actual report

**When:** After each `GeminiGenerator.generate()` call, from `backend/app/clients.py:310` (where `GeminiCostTracker.record()` is currently called). Each call within a session is a separate actual report with the same `intent_id`.

```json
POST /api/v1/actual
Authorization: Bearer <token>
{
  "intent_id": "int_a1b2c3d4e5f6",
  "project_id": "your-project-1",
  "job_id": "<session_id>",
  "provider": "gemini",
  "api": "models.generate_content",
  "actual_calls": 1,
  "actual_cost_usd": 0.0003,
  "actual_tokens": 1500,
  "status": "completed",
  "started_at": "2026-07-24T18:00:00Z",
  "ended_at": "2026-07-24T18:00:02Z"
}
```

**Field mapping from existing code** (`backend/app/costs.py:121-132`):

| CloudManagement field | Existing Firestore `api_costs` field | Source |
|---|---|---|
| `actual_tokens` | `inputTokens` + `outputTokens` | `usage.prompt_token_count` + `response_tokens + thoughts_tokens` |
| `actual_cost_usd` | `estimatedCostUsd` | `GeminiCostTracker.estimate(input, output)` |
| `status` | (new) | `"completed"` on success, `"failed"` on exception |
| `metadata.model` | `model` | `self.settings.gemini_model` |

**Incremental reporting:** For a single `generate_content()` call, one actual report suffices (status `completed`). If a session makes multiple Gemini calls, each is a separate actual report with `status: "completed"` and `sequence` auto-incremented by CloudManagement.

### 1.4 Twilio intent + actual (only if `TWILIO_ENABLED=true`)

**When:** Before `send_sms()` in `backend/functions/webhook_dispatcher/main.py:98`.

```json
POST /api/v1/intent
{
  "project_id": "your-project-1",
  "source_repo": "your-org/AIRichardMoon",
  "job_id": "sms_<timestamp>_<to_number>",
  "job_name": "coaching-sms",
  "provider": "twilio",
  "api": "Messages.json",
  "expected_calls": 1,
  "expected_cost_usd": 0.0079,
  "rate_limit_rpm": 10,
  "window_start": "2026-07-24T18:00:00Z",
  "window_end": "2026-07-24T18:00:01Z",
  "kill": {
    "type": "http_callback",
    "url": "https://<cloud-run-url>/v1/admin/kill-sms",
    "method": "POST",
    "headers": {"Authorization": "Bearer <DASHBOARD_ADMIN_KEY>"}
  },
  "metadata": {
    "to_number": "<normalized>",
    "body_length": 160
  }
}
```

**Actual:**
```json
POST /api/v1/actual
{
  "intent_id": "int_...",
  "project_id": "your-project-1",
  "job_id": "sms_<timestamp>_<to_number>",
  "provider": "twilio",
  "api": "Messages.json",
  "actual_calls": 1,
  "actual_cost_usd": 0.0079,
  "status": "completed"
}
```

### 1.5 Kill endpoint (to implement in AIRichardMoon)

**Route:** `POST /v1/admin/kill-session` in `backend/app/main.py`
**Auth:** `Authorization: Bearer <DASHBOARD_ADMIN_KEY>`
**Behavior:** Marks the session as killed in Firestore, cancels any in-flight Gemini call (requires adding a `timeout` to `generate_content()` and a per-session cancellation token).
**Response:**
```json
{"killed": true, "job_id": "<session_id>", "reason": "cost_overrun"}
```

### 1.6 Expected costs pull

```json
GET /api/v1/expected-costs/your-project-1
```
**Response:**
```json
{
  "project_id": "your-project-1",
  "updated_at": "2026-07-24T16:00:00Z",
  "providers": {
    "gemini": {
      "unit_cost_usd": 0.0003,
      "free_tier_remaining_calls": null,
      "free_tier_reset": "",
      "expected_remaining_monthly_usd": 8.50,
      "calibration_delta": 0.0,
      "pricing": {
        "input_cost_per_million": 0.10,
        "output_cost_per_million": 0.40,
        "model": "gemini-3.1-flash-lite"
      }
    },
    "twilio": {
      "unit_cost_usd": 0.0079,
      "free_tier_remaining_calls": null,
      "expected_remaining_monthly_usd": 2.00,
      "calibration_delta": 0.0,
      "pricing": {"per_sms_usd": 0.0079}
    }
  }
}
```

AIRichardMoon should update `GEMINI_INPUT_COST_PER_MILLION_USD` / `GEMINI_OUTPUT_COST_PER_MILLION_USD` in `backend/app/config.py` from this response every 15 min (Cloud Scheduler).

---

## 2. WorldStudioFinder (`your-org/WorldStudioFinder`)

**CloudManagement project_id:** `your-project-2` (primary, GCP project `your-gcp-project-1`) and `your-project-2-alt` (alt, GCP project `your-gcp-project-2`)
**Ticket:** https://github.com/your-org/WorldStudioFinder/issues/162

### 2.1 Paid API calls (from code analysis)

| # | Provider | API | Call site | Cost model | Free tier |
|---|----------|-----|-----------|------------|-----------|
| 1 | `google_places` | `places:searchText` | `src/scrapers/google_places_api.py:290` | $0.035/call | 1,000/mo |
| 2 | `google_places` | `places/{place_id}` | `src/scrapers/google_places_api.py:394` | $0.035/call (Place Details) | shared |
| 3 | `google_geocoding` | `geocode/json` | `src/geocoding/providers/google.py:72` | $0.005/call (after free) | 40,000/mo |
| 4 | `google_geocoding` | `geocode/json` (reverse) | `src/geocoding/providers/google.py:119` | $0.005/call | shared |
| 5 | `google_kg` | `entities:search` | `src/discovery/knowledge_graph_client.py:85` | Free | 100,000/day |
| 6 | `here` | `discover` | `src/scrapers/here_client.py:49` | Free tier then paid | 1,000/day |
| 7 | `azure_maps` | `search/fuzzy` | `src/scrapers/azure_maps_client.py:157` | $0.0045/call (after free) | 5,000/mo |
| 8 | `opencage` | `geocode/v1/json` | `src/geocoding/providers/opencage.py:55` | Free tier then paid | 2,500/day |
| 9 | `brave` | `web/search` | `src/discovery/social_hunt.py:409` | $0.003/call (after free) | 2,000/mo |
| 10 | `facebook_graph` | `v20.0/{path}` | `src/discovery/facebook_graph.py:397` | Free (rate-limited) | 200/hr/token |
| 11 | `phantombuster` | `agents/launch` | `src/discovery/phantom_client.py:85` | Per-launch (plan-based) | 25/day |
| 12 | `gemini` | `generate_content` (realtime) | `src/ai/gemini_batch_client.py:156` | $0.10/1M in, $0.40/1M out | — |
| 13 | `gemini` | `batch_create` | `src/ai/gemini_batch_client.py:244` | $0.05/1M in, $0.20/1M out (50% off) | — |
| 14 | `gemini` | `cache.create` | `src/ai/context_cache.py:71` | Reduces per-call cost | — |

**Not reported (free/no cost):** Google Knowledge Graph (free 100K/day), Facebook Graph (free, rate-limited), PhantomBuster (plan-based, not per-call). These should still be tracked for volume but don't need intent/actual for cost control. Add them to `api_usage_events` only.

### 2.2 Intent declarations

WorldStudioFinder has **multiple API call phases** within a single scrape run. Each phase is a separate intent. The `job_id` format is `{pipeline}_{phase}_{timestamp}` (e.g., `wsf_scrape_20260724_180022`).

#### 2.2.1 Google Places Text Search (highest spend — $0.035/call)

**When:** Before each `GooglePlacesAPI.search_nearby()` call batch in `src/scrapers/google_places_api.py:290`.

```json
POST /api/v1/intent
{
  "project_id": "your-project-2",
  "source_repo": "your-org/WorldStudioFinder",
  "job_id": "wsf_scrape_20260724_180022",
  "job_name": "scrape-phase1-places",
  "provider": "google_places",
  "api": "places:searchText",
  "expected_calls": 60,
  "expected_cost_usd": 2.10,
  "rate_limit_rpm": 100,
  "window_start": "2026-07-24T18:00:22Z",
  "window_end": "2026-07-24T18:30:00Z",
  "kill": {
    "type": "http_callback",
    "url": "https://<your-project-2-url>/admin/kill-job",
    "method": "POST",
    "headers": {"X-Kill-Token": "<set via Secret Manager>"}
  },
  "metadata": {
    "city": "Auburn, ME",
    "category": "yoga_studio",
    "max_results": 60,
    "session_budget_usd": 10.0,
    "monthly_free_remaining": 850
  }
}
```

**Expected cost computation** (from `src/scrapers/google_places_api.py:54` QuotaBudget):
```
expected_calls = max_results / 20  (20 results per page, up to 3 pages = 3 calls)
expected_cost_usd = expected_calls * 0.035
```

#### 2.2.2 Google Places Place Details

```json
POST /api/v1/intent
{
  "project_id": "your-project-2",
  "job_id": "wsf_scrape_20260724_180022",
  "job_name": "scrape-phase1-details",
  "provider": "google_places",
  "api": "places/{place_id}",
  "expected_calls": 60,
  "expected_cost_usd": 2.10,
  "rate_limit_rpm": 100,
  "window_start": "2026-07-24T18:05:00Z",
  "window_end": "2026-07-24T18:20:00Z",
  "kill": {"type": "http_callback", "url": "https://<url>/admin/kill-job", "method": "POST", "headers": {"X-Kill-Token": "<...>"}},
  "metadata": {"place_count": 60}
}
```

#### 2.2.3 Google Geocoding

```json
POST /api/v1/intent
{
  "project_id": "your-project-2",
  "job_id": "wsf_scrape_20260724_180022",
  "job_name": "geocode-phase",
  "provider": "google_geocoding",
  "api": "geocode/json",
  "expected_calls": 60,
  "expected_cost_usd": 0.30,
  "rate_limit_rpm": 50,
  "window_start": "2026-07-24T18:20:00Z",
  "window_end": "2026-07-24T18:25:00Z",
  "kill": {"type": "http_callback", "url": "https://<url>/admin/kill-job", "method": "POST", "headers": {"X-Kill-Token": "<...>"}},
  "metadata": {"addresses_to_geocode": 60}
}
```

#### 2.2.4 Gemini batch (realtime fallback)

**When:** Before `GeminiBatchClient.generate_realtime()` in `src/ai/gemini_batch_client.py:156`.

```json
POST /api/v1/intent
{
  "project_id": "your-project-2",
  "job_id": "wsf_enrich_20260724_183022",
  "job_name": "gemini-enrich-batch",
  "provider": "gemini",
  "api": "generate_content",
  "expected_calls": 45,
  "expected_cost_usd": 0.05,
  "expected_tokens": 90000,
  "rate_limit_rpm": 60,
  "window_start": "2026-07-24T18:30:22Z",
  "window_end": "2026-07-24T18:45:00Z",
  "kill": {"type": "http_callback", "url": "https://<url>/admin/kill-job", "method": "POST", "headers": {"X-Kill-Token": "<...>"}},
  "metadata": {
    "model": "gemini-2.0-flash-001",
    "stage": "enrich",
    "prompt_count": 45,
    "cached_content": "projects/.../cachedContents/..."
  }
}
```

**Expected cost** (from `src/costs/gemini_cost_tracker.py:281`):
```
expected_cost = (expected_input_tokens * 0.10 + expected_output_tokens * 0.40) / 1_000_000
```

#### 2.2.5 Brave Search

```json
POST /api/v1/intent
{
  "project_id": "your-project-2",
  "job_id": "wsf_discover_20260724_190022",
  "job_name": "brave-social-hunt",
  "provider": "brave",
  "api": "web/search",
  "expected_calls": 30,
  "expected_cost_usd": 0.09,
  "rate_limit_rpm": 60,
  "window_start": "2026-07-24T19:00:22Z",
  "window_end": "2026-07-24T19:10:00Z",
  "kill": {"type": "http_callback", "url": "https://<url>/admin/kill-job", "method": "POST", "headers": {"X-Kill-Token": "<...>"}},
  "metadata": {"queries": 30, "results_per_query": 3}
}
```

#### 2.2.6 HERE / Azure Maps / OpenCage (free-tier fallback APIs)

These are used by the `MultiSourceAPI` free-tier router (`src/scrapers/multi_source_api.py`). Declare intent when the free-tier router selects them:

```json
POST /api/v1/intent
{
  "project_id": "your-project-2",
  "job_id": "wsf_scrape_20260724_180022",
  "job_name": "here-fallback-search",
  "provider": "here",
  "api": "discover",
  "expected_calls": 20,
  "expected_cost_usd": 0.0,
  "rate_limit_rpm": 30,
  "window_start": "2026-07-24T18:02:00Z",
  "window_end": "2026-07-24T18:10:00Z",
  "kill": {"type": "http_callback", "url": "https://<url>/admin/kill-job", "method": "POST", "headers": {"X-Kill-Token": "<...>"}},
  "metadata": {"free_tier_remaining_today": 850, "reason": "google_places_budget_exhausted"}
}
```

Same format for `azure_maps` and `opencage` — just change `provider` and `api`.

### 2.3 Actual reports

**When:** After each API call batch, from the existing `log_api_usage()` call sites. Each `log_api_usage(provider, endpoint, units)` in `src/utils/api_usage.py:30` should be mirrored to a CloudManagement actual report.

#### Google Places actual (incremental during long scrape)

```json
POST /api/v1/actual
{
  "intent_id": "int_...",
  "project_id": "your-project-2",
  "job_id": "wsf_scrape_20260724_180022",
  "provider": "google_places",
  "api": "places:searchText",
  "actual_calls": 20,
  "actual_cost_usd": 0.70,
  "status": "running",
  "started_at": "2026-07-24T18:00:22Z",
  "ended_at": "2026-07-24T18:05:00Z"
}
```

**Field mapping from existing SQLite tables:**

| CloudManagement field | Existing SQLite source | Table |
|---|---|---|
| `actual_calls` | `units` (summed per provider+endpoint) | `api_usage_events` |
| `actual_cost_usd` | `count * paid_cost_per_unit` (from `ApiUsageSettings`) | computed from `api_usage_events` + `DEFAULT_API_USAGE_PROVIDERS` |
| `status` | (new) | — |
| `metadata.remaining_credits` | `remaining_credits` | `api_call_log` |
| `metadata.period_used_after` | `period_used_after` | `api_call_log` |

#### Gemini actual

```json
POST /api/v1/actual
{
  "intent_id": "int_...",
  "project_id": "your-project-2",
  "job_id": "wsf_enrich_20260724_183022",
  "provider": "gemini",
  "api": "generate_content",
  "actual_calls": 45,
  "actual_cost_usd": 0.048,
  "actual_tokens": 85000,
  "status": "completed",
  "started_at": "2026-07-24T18:30:22Z",
  "ended_at": "2026-07-24T18:42:00Z"
}
```

**Field mapping from `gemini_cost_records` table:**

| CloudManagement field | SQLite column | Source |
|---|---|---|
| `actual_tokens` | `input_tokens` + `output_tokens` | `usage.prompt_token_count` + `usage.candidates_token_count` |
| `actual_cost_usd` | `estimated_cost_usd` | `GeminiCostTracker.estimate()` |
| `metadata.model` | `model` | config |
| `metadata.stage` | `stage` | passed to `generate_realtime()` |

### 2.4 Kill endpoint (to implement in WorldStudioFinder)

**Route:** `POST /admin/kill-job` in `web/app.py` (Flask)
**Auth:** `X-Kill-Token` header (set via Secret Manager)
**Behavior:**
1. Set a `cancelled` flag in a new `job_control` table in `pipeline.db` (the scraper polls this every N iterations)
2. If the job is a systemd service, call `systemctl stop scraper-phase1.service`
3. Return confirmation

```json
// Request (from CloudManagement)
POST /admin/kill-job
X-Kill-Token: <token>
{"reason": "actual_exceeds_intent_calls", "job_id": "wsf_scrape_20260724_180022"}

// Response
{"killed": true, "job_id": "wsf_scrape_20260724_180022", "action": "flag_set+systemd_stop"}
```

**Scraper polling:** Add a check at the top of each scraper loop iteration:
```python
if is_job_cancelled(job_id):
    log.info("Job cancelled by CloudManagement, exiting gracefully")
    break
```

### 2.5 Expected costs pull

```json
GET /api/v1/expected-costs/your-project-2
```
**Response:**
```json
{
  "project_id": "your-project-2",
  "updated_at": "2026-07-24T16:00:00Z",
  "providers": {
    "google_places": {
      "unit_cost_usd": 0.035,
      "free_tier_remaining_calls": 850,
      "free_tier_reset": "2026-08-01",
      "expected_remaining_monthly_usd": 22.75,
      "calibration_delta": 0.0,
      "pricing": {"per_call_usd": 0.035, "free_tier_monthly": 1000}
    },
    "google_geocoding": {
      "unit_cost_usd": 0.005,
      "free_tier_remaining_calls": 38500,
      "free_tier_reset": "2026-08-01",
      "expected_remaining_monthly_usd": 0.0,
      "calibration_delta": 0.0,
      "pricing": {"per_call_usd": 0.005, "free_tier_monthly": 40000}
    },
    "gemini": {
      "unit_cost_usd": 0.001,
      "free_tier_remaining_calls": null,
      "expected_remaining_monthly_usd": 15.0,
      "calibration_delta": 0.0,
      "pricing": {
        "input_cost_per_million": 0.10,
        "output_cost_per_million": 0.40,
        "batch_input_cost_per_million": 0.05,
        "batch_output_cost_per_million": 0.20
      }
    },
    "brave": {
      "unit_cost_usd": 0.003,
      "free_tier_remaining_calls": 1850,
      "free_tier_reset": "2026-08-01",
      "expected_remaining_monthly_usd": 0.0,
      "calibration_delta": 0.0,
      "pricing": {"per_call_usd": 0.003, "free_tier_monthly": 2000}
    },
    "here": {
      "unit_cost_usd": 0.0,
      "free_tier_remaining_calls": 850,
      "free_tier_reset": "2026-07-25",
      "expected_remaining_monthly_usd": 0.0,
      "calibration_delta": 0.0,
      "pricing": {"per_call_usd": 0.0, "free_tier_daily": 1000}
    },
    "azure_maps": {
      "unit_cost_usd": 0.0045,
      "free_tier_remaining_calls": 4800,
      "free_tier_reset": "2026-08-01",
      "expected_remaining_monthly_usd": 0.0,
      "calibration_delta": 0.0,
      "pricing": {"per_call_usd": 0.0045, "free_tier_monthly": 5000}
    },
    "opencage": {
      "unit_cost_usd": 0.0,
      "free_tier_remaining_calls": 2300,
      "free_tier_reset": "2026-07-25",
      "expected_remaining_monthly_usd": 0.0,
      "calibration_delta": 0.0,
      "pricing": {"per_call_usd": 0.0, "free_tier_daily": 2500}
    }
  }
}
```

WorldStudioFinder should update `CostTrackingConfig` and `DEFAULT_API_USAGE_PROVIDERS` in `config/settings.py` from this response every 15 min.

---

## 3. FieldWorker (`your-org/FieldWorker`)

**CloudManagement project_id:** `your-project-3`
**Cloud:** OpenStack (your-openstack-provider)
**OpenStack project:** `your-openstack-project-id` (ID: `your-openstack-uuid`)
**Regions:** `your-region-1` (Mumbai), `your-region-2` (US East)
**Ticket:** https://github.com/your-org/FieldWorker/issues/231

### 3.1 Paid resource (from code analysis)

| # | Provider | API | Call site | Cost model |
|---|----------|-----|-----------|------------|
| 1 | `openstack` | `compute.instance_hours` | `openstack_shutdown_instances.sh:171` (start) / `:91` (stop) | Per-instance-hour (~$0.01-0.05/hr depending on flavor) |
| 2 | `openstack` | `volume.gb_hours` | (implicit — volumes attached to instances) | Per-GB-hour |
| 3 | `openstack` | `snapshot.gb` | `openstack_create_snapshots.sh:49` | Per-GB-month |

**No existing cost tracking.** This is greenfield — no SQLite table, no cost tracker, no instance-hours logging.

### 3.2 Intent declaration (instance start)

**When:** Before `openstack_shutdown_instances.sh start` or any manual instance start. The natural `job_id` is the instance lifecycle: `os_instance_<instance_id>_<start_timestamp>`.

```json
POST /api/v1/intent
{
  "project_id": "your-project-3",
  "source_repo": "your-org/FieldWorker",
  "job_id": "os_instance_a1b2c3d4_20260724180022",
  "job_name": "your-project-3-prod-instance",
  "provider": "openstack",
  "api": "compute.instance_hours",
  "expected_calls": 8,
  "expected_cost_usd": 0.32,
  "rate_limit_rpm": 0,
  "window_start": "2026-07-24T18:00:22Z",
  "window_end": "2026-07-25T02:00:22Z",
  "kill": {
    "type": "openstack",
    "instance_id": "<instance_uuid>",
    "region": "your-region-1"
  },
  "metadata": {
    "instance_name": "your-project-3-prod",
    "flavor": "gp.1c1r",
    "region": "your-region-1",
    "expected_hours": 8,
    "hourly_cost_usd": 0.04
  }
}
```

**Expected cost computation:**
```
expected_calls = expected_hours
expected_cost_usd = expected_hours * hourly_cost_usd
```
The `hourly_cost_usd` must be configured per flavor (your-openstack-provider pricing). CloudManagement's reconciliation tier will correct this against actual metering.

### 3.3 Actual report (instance running — daily heartbeat)

**When:** Daily while the instance is running (Cloud Scheduler or cron on the instance itself).

```json
POST /api/v1/actual
{
  "intent_id": "int_...",
  "project_id": "your-project-3",
  "job_id": "os_instance_a1b2c3d4_20260724180022",
  "provider": "openstack",
  "api": "compute.instance_hours",
  "actual_calls": 24,
  "actual_cost_usd": 0.96,
  "status": "running",
  "started_at": "2026-07-24T18:00:22Z",
  "ended_at": "2026-07-25T18:00:22Z"
}
```

### 3.4 Actual report (instance stop — final)

**When:** When `openstack_shutdown_instances.sh stop` is executed.

```json
POST /api/v1/actual
{
  "intent_id": "int_...",
  "project_id": "your-project-3",
  "job_id": "os_instance_a1b2c3d4_20260724180022",
  "provider": "openstack",
  "api": "compute.instance_hours",
  "actual_calls": 8.5,
  "actual_cost_usd": 0.34,
  "status": "completed",
  "started_at": "2026-07-24T18:00:22Z",
  "ended_at": "2026-07-25T02:30:00Z"
}
```

### 3.5 Kill endpoint (to implement in FieldWorker)

**Route:** `POST /admin/kill-job` in `your-project-3/admin.py` (Flask, using existing `@admin_required` decorator)
**Auth:** Admin session + `X-Kill-Token` header
**Behavior:** Calls `subprocess.run(['./openstack_shutdown_instances.sh', 'stop', '--force'])` (the existing script at `openstack_shutdown_instances.sh:91`).

```python
@admin_bp.route("/kill-job", methods=["POST"])
@admin_required
def kill_job():
    data = request.get_json()
    job_id = data.get("job_id", "")
    reason = data.get("reason", "cost_overrun")

    # Stop the specific instance if instance_id provided,
    # otherwise stop all instances
    instance_id = data.get("instance_id")
    region = data.get("region", "your-region-1")

    if instance_id:
        result = subprocess.run(
            ["openstack", "server", "stop", instance_id],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "OS_REGION_NAME": region}
        )
    else:
        result = subprocess.run(
            ["./openstack_shutdown_instances.sh", "stop", "--force"],
            capture_output=True, text=True, timeout=120
        )

    return jsonify({
        "killed": result.returncode == 0,
        "job_id": job_id,
        "detail": result.stdout[:500],
        "error": result.stderr[:200] if result.returncode else ""
    })
```

**Response:**
```json
{"killed": true, "job_id": "os_instance_a1b2c3d4_20260724180022", "detail": "Stopped instance a1b2c3d4"}
```

### 3.6 Expected costs pull

```json
GET /api/v1/expected-costs/your-project-3
```
**Response:**
```json
{
  "project_id": "your-project-3",
  "updated_at": "2026-07-24T16:00:00Z",
  "providers": {
    "openstack": {
      "unit_cost_usd": 0.04,
      "free_tier_remaining_calls": null,
      "free_tier_reset": "",
      "expected_remaining_monthly_usd": 15.0,
      "calibration_delta": 0.002,
      "pricing": {
        "flavors": {
          "gp.1c1r": {"hourly_usd": 0.04},
          "gp.2c2r": {"hourly_usd": 0.08},
          "gp.2c4r": {"hourly_usd": 0.12}
        },
        "volume_gb_hourly_usd": 0.000003,
        "snapshot_gb_monthly_usd": 0.05
      }
    }
  }
}
```

The `calibration_delta` is set by the reconciliation tier when OpenStack metering data is available (currently not implemented — see `providers/openstack.py` TODO for Ceilometer/Gnocchi).

---

## 4. FieldAppAndroid (`your-org/FieldAppAndroid`)

**CloudManagement project_id:** `your-project-3` (shares FieldWorker's backend)
**Ticket:** https://github.com/your-org/FieldAppAndroid/issues/1

### 4.1 API calls

**NONE.** Confirmed by code analysis: the Android client makes zero direct paid API calls. All network traffic goes to the FieldWorker backend via:
- `AuthApi.kt` — auth endpoints (free)
- `GpsApi.kt` — GPS endpoints (free, backend)
- `ActivityApi.kt` — activity endpoints (free, backend)

`GpsTrackingService` uses `FusedLocationProviderClient` (free Android SDK for GPS hardware). No cloud API calls.

### 4.2 No intent/actual reporting needed

This repo requires **no CloudManagement integration**. The ticket (#1) is for confirmation and documentation only. All costs are covered by the FieldWorker ticket (#231).

---

## 5. Security (`your-org/your-security-repo`)

**CloudManagement project_id:** `your-project-5`
**GCP project:** `your-gcp-project-1` (shared with WorldStudioFinder)
**Ticket:** https://github.com/your-org/your-security-repo/issues/73

### 5.1 Paid resource (from code analysis)

| # | Provider | API | Call site | Cost model |
|---|----------|-----|-----------|------------|
| 1 | `gcp_compute` | `compute.instance_hours` | `UnusedOS/scripts/run_vm_build_deploy_test.sh:38` (`terraform apply`) | ~$0.06/hr for e2-small |
| 2 | `gcp_compute` | `disk.gb_hours` | (implicit — 20GB boot disk) | ~$0.000084/GB-hr |

**Not reported (free):** GithubLeak (`githubleak.py:42` — free GitHub REST API, 5000 req/hr), Truffle (`app.py:298` — free GitHub CLI), Cloudflare (`DeployMaintenace.sh:42` — free Pages/DNS).

### 5.2 Intent declaration (VM creation)

**When:** Before `terraform apply` in `UnusedOS/scripts/run_vm_build_deploy_test.sh:38`.

```json
POST /api/v1/intent
{
  "project_id": "your-project-5",
  "source_repo": "your-org/your-security-repo",
  "job_id": "unusedos_test_20260724180022",
  "job_name": "unusedos-vm-build-test",
  "provider": "gcp_compute",
  "api": "compute.instance_hours",
  "expected_calls": 1,
  "expected_cost_usd": 0.06,
  "rate_limit_rpm": 0,
  "window_start": "2026-07-24T18:00:22Z",
  "window_end": "2026-07-24T19:00:22Z",
  "kill": {
    "type": "gce",
    "project_id": "your-gcp-project-1",
    "instance": "unusedos-test-vm",
    "zone": "us-central1-a"
  },
  "metadata": {
    "machine_type": "e2-small",
    "disk_gb": 20,
    "region": "us-central1",
    "zone": "us-central1-a",
    "expected_hours": 1
  }
}
```

**Expected cost:**
```
expected_cost_usd = expected_hours * e2_small_hourly + disk_gb * 720 * 0.000084
                  = 1 * 0.06 + 20 * 720 * 0.000084
                  = 0.06 + 1.21 = ~$1.27/mo if left running
```

### 5.3 Actual report (VM teardown)

**When:** After `terraform destroy` in `run_vm_build_deploy_test.sh:70`.

```json
POST /api/v1/actual
{
  "intent_id": "int_...",
  "project_id": "your-project-5",
  "job_id": "unusedos_test_20260724180022",
  "provider": "gcp_compute",
  "api": "compute.instance_hours",
  "actual_calls": 0.8,
  "actual_cost_usd": 0.05,
  "status": "completed",
  "started_at": "2026-07-24T18:00:22Z",
  "ended_at": "2026-07-24T18:48:00Z"
}
```

### 5.4 Kill endpoint

The kill descriptor uses GCP-native `gce` type, so **no HTTP endpoint needed in the Security repo**. CloudManagement's `GcpProvider._kill_gce()` calls `compute_v1.InstancesClient().stop()` directly.

If an HTTP callback is preferred instead:
```json
"kill": {
  "type": "http_callback",
  "url": "https://<security-webhook-url>/admin/kill-job",
  "method": "POST",
  "headers": {"X-Kill-Token": "<...>"},
  "body": {"action": "terraform_destroy", "dir": "UnusedOS/terraform-test"}
}
```

### 5.5 Expected costs pull

```json
GET /api/v1/expected-costs/your-project-5
```
**Response:**
```json
{
  "project_id": "your-project-5",
  "updated_at": "2026-07-24T16:00:00Z",
  "providers": {
    "gcp_compute": {
      "unit_cost_usd": 0.06,
      "free_tier_remaining_calls": null,
      "expected_remaining_monthly_usd": 0.0,
      "calibration_delta": 0.0,
      "pricing": {
        "e2_small_hourly_usd": 0.06,
        "e2_standard_4_hourly_usd": 0.24,
        "disk_gb_hourly_usd": 0.000084
      }
    }
  }
}
```

---

## 6. ClipQuotes (`your-org/ClipQuotes`)

**CloudManagement project_id:** `your-project-4`
**Ticket:** https://github.com/your-org/ClipQuotes/issues/42

### 6.1 Paid API calls (from code analysis)

| # | Provider | API | Call site | Cost model | Status |
|---|----------|-----|-----------|------------|--------|
| 1 | `huggingface` | `Pipeline.from_pretrained` | `settings_manager/pyannote_backend.py:104` | Free (model download) | Free tier |
| 2 | `huggingface` | `DiarizationPipeline` | `settings_manager/whisperx_backend.py:142` | Free (model download) | Free tier |
| 3 | `cloudflare_r2` | `storage.gb_hours` | (not integrated — `setup_rclone_r2.sh` exists but unused) | $0.015/GB-mo | **Not active** |

**Conclusion from code analysis:** ClipQuotes currently has **zero paid API costs**. HuggingFace models are downloaded free and run locally. R2 is provisioned via a setup script but not integrated into the app.

### 6.2 Intent declaration (only if HF paid inference or R2 is activated)

#### HuggingFace inference (if paid endpoints are used in the future)

```json
POST /api/v1/intent
{
  "project_id": "your-project-4",
  "source_repo": "your-org/ClipQuotes",
  "job_id": "clip_1750000000",
  "job_name": "clip-extraction",
  "provider": "huggingface",
  "api": "inference",
  "expected_calls": 1,
  "expected_cost_usd": 0.02,
  "expected_tokens": 50000,
  "rate_limit_rpm": 30,
  "window_start": "2026-07-24T18:00:00Z",
  "window_end": "2026-07-24T18:10:00Z",
  "kill": {
    "type": "http_callback",
    "url": "http://localhost:5000/api/batch/cancel",
    "method": "POST",
    "headers": {"Content-Type": "application/json"}
  },
  "metadata": {
    "model": "pyannote/speaker-diarization-3.1",
    "audio_duration_seconds": 600
  }
}
```

**job_id:** The existing `job_{int(time.time())}` format from `settings_manager/job_runner.py:162`.

#### Cloudflare R2 storage (if activated)

```json
POST /api/v1/intent
{
  "project_id": "your-project-4",
  "job_id": "r2_storage_daily_20260724",
  "job_name": "r2-storage-daily",
  "provider": "cloudflare_r2",
  "api": "storage.gb_hours",
  "expected_calls": 5,
  "expected_cost_usd": 0.075,
  "rate_limit_rpm": 0,
  "window_start": "2026-07-24T00:00:00Z",
  "window_end": "2026-07-25T00:00:00Z",
  "kill": {"type": "cloudflare", "action": "disable_pages", "project": "your-project-4"},
  "metadata": {"expected_gb": 5, "monthly_cost_per_gb": 0.015}
}
```

### 6.3 Kill endpoint (already exists!)

ClipQuotes already has a kill endpoint at `settings_manager/app.py:1012`:

**Route:** `POST /api/batch/cancel`
**Behavior:** Calls `cancel_batch()` in `batch_workflow.py:368` which:
1. Sets `batch_state.stage = "cancelled"` (with threading lock)
2. Calls `_current_process.terminate()` (SIGTERM to subprocess)

**Response:**
```json
{"killed": true, "job_id": "clip_1750000000"}
```

This is the kill descriptor's `url` in the intent — CloudManagement calls it directly.

### 6.4 Expected costs pull

```json
GET /api/v1/expected-costs/your-project-4
```
**Response:**
```json
{
  "project_id": "your-project-4",
  "updated_at": "2026-07-24T16:00:00Z",
  "providers": {
    "huggingface": {
      "unit_cost_usd": 0.0,
      "free_tier_remaining_calls": null,
      "expected_remaining_monthly_usd": 0.0,
      "calibration_delta": 0.0,
      "pricing": {"model_download": "free", "inference_per_1k_tokens": 0.002}
    },
    "cloudflare_r2": {
      "unit_cost_usd": 0.015,
      "free_tier_remaining_calls": null,
      "expected_remaining_monthly_usd": 0.0,
      "calibration_delta": 0.0,
      "pricing": {"per_gb_monthly": 0.015, "class_a_operations_per_1k": 4.5, "class_b_operations_per_1k": 0.36}
    }
  }
}
```

---

## 7. Summary: Kill descriptor types per repo

| Repo | Kill type | Mechanism | Endpoint |
|---|---|---|---|
| AIRichardMoon | `http_callback` | Cancel in-flight Gemini call | `POST /v1/admin/kill-session` (to implement) |
| WorldStudioFinder | `http_callback` | DB flag + systemd stop | `POST /admin/kill-job` (to implement) |
| FieldWorker | `openstack` | `openstack server stop` | CloudManagement calls OpenStack directly (or `POST /admin/kill-job` wrapper) |
| FieldAppAndroid | N/A | No paid APIs | — |
| Security | `gce` | `compute_v1.InstancesClient().stop()` | CloudManagement calls GCP directly |
| ClipQuotes | `http_callback` | `subprocess.terminate()` | `POST /api/batch/cancel` (already exists) |

## 8. Summary: Intent declaration frequency per repo

| Repo | # intent types | When declared | Granularity |
|---|---|---|---|
| AIRichardMoon | 2 (gemini, twilio) | Before each coaching session / SMS | Per session |
| WorldStudioFinder | 7+ (places, details, geocoding, gemini, brave, here, azure, opencage) | Before each scrape phase | Per phase per city/category |
| FieldWorker | 1 (openstack) | Before instance start | Per instance lifecycle |
| FieldAppAndroid | 0 | — | — |
| Security | 1 (gcp_compute) | Before terraform apply | Per test run |
| ClipQuotes | 0-2 (huggingface, r2) | Only if paid features activated | Per clip extraction / daily |
