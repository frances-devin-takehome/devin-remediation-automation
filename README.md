# devin-remediation-automation

Python service that will orchestrate automated engineering remediation using GitHub webhooks and
the Devin API. A GitHub issue labeled `devin-remediation` in the allowed repository triggers a
Devin session that remediates the issue.

## Requirements

- Python 3.12

## Configuration

Configuration is read from the process environment (see `.env.example` for the full list):

| Variable | Required | Description |
| --- | --- | --- |
| `GITHUB_WEBHOOK_SECRET` | yes | Shared secret configured on the GitHub webhook, used to verify the `X-Hub-Signature-256` header. |
| `DEVIN_API_KEY` | yes | API key of a Devin service user with the `ManageOrgSessions` permission. Sent as `Authorization: Bearer`. |
| `DEVIN_ORG_ID` | yes | Devin organization ID used in the v3 Organization API path. |
| `DEVIN_API_BASE_URL` | no | Devin API base URL. Defaults to `https://api.devin.ai`. |
| `ALLOWED_REPOSITORY` | no | Only issues from this `owner/name` repository dispatch a session. Defaults to `frances-devin-takehome/superset`. |
| `DELIVERY_DB_PATH` | no | SQLite file recording remediation jobs (idempotency + lifecycle state). Defaults to `data/deliveries.db` (`/data/deliveries.db` in the Docker image). |

Credentials are read from the process environment only; they are never committed or baked into
the Docker image.

## Local development

Create a virtual environment and install the project with development dependencies:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run the service:

```bash
export GITHUB_WEBHOOK_SECRET=replace-me
export DEVIN_API_KEY=replace-me
export DEVIN_ORG_ID=replace-me
uvicorn devin_remediation_automation.main:app --reload --port 8000
```

Check the health endpoint:

```bash
curl http://localhost:8000/health
# {"status":"healthy"}
```

Interactive API docs are available at http://localhost:8000/docs.

## Endpoints

- `GET /health` — liveness check.
- `POST /webhooks/github` — GitHub webhook receiver. Other valid events return
  `{"status": "ignored"}`; an invalid or missing signature returns `401`.
- `GET /remediations` — read-only list of remediation jobs, newest first. Supports
  `?status=in_progress|dispatched|failed`, `?limit=` (1–200, default 50) and `?offset=`.
- `GET /remediations/metrics` — aggregate counts and operational timestamps.
- `GET /remediations/{delivery_id}` — a single job, or `404` if the delivery is unknown.

## Event → Devin session flow

1. GitHub delivers an `issues` event; the `X-Hub-Signature-256` header is verified against
   `GITHUB_WEBHOOK_SECRET`.
2. The event is eligible only when `action` is `labeled`, the added label is exactly
   `devin-remediation`, and the repository full name equals `ALLOWED_REPOSITORY`. Anything else is
   ignored without calling Devin.
3. A task is built from the payload (repository full name, issue number, title, URL, and issue
   body/acceptance criteria) asking Devin to investigate, make the smallest appropriate fix, run
   the relevant validation, and open a pull request. The fix itself is not prescribed.
4. The delivery is claimed in SQLite by its `X-GitHub-Delivery` id (see Idempotency below); an
   already-handled delivery returns without calling Devin.
5. One session is created via the Devin v3 Organization API
   (`POST {DEVIN_API_BASE_URL}/v3/organizations/{DEVIN_ORG_ID}/sessions`). On success the response
   is `{"status": "dispatched", "devin_session_id": ..., "devin_session_url": ...}` and the
   repository, issue, and session identifiers are logged (never credentials).
6. If session creation fails, the endpoint returns `502` and the event is not reported as
   dispatched.

Tests use a mocked Devin API and never create real sessions.

## Remediation state and observability

Each eligible delivery is persisted as a remediation job in the `remediation_jobs` SQLite table:
GitHub delivery id (primary key), repository, issue number/title/URL, status, Devin session
id/URL once created, dispatch attempt count, last failure message, and `created_at` /
`updated_at` / `dispatched_at` timestamps.

Statuses are `in_progress` (a dispatch attempt is running), `dispatched` (a Devin session was
created) and `failed` (the Devin API call failed; the delivery stays retryable).
**`dispatched` is not a successful remediation** — the service does not yet track the session
outcome or the resulting pull request, so operators should read it as "handed to Devin".

`GET /remediations/metrics` returns:

```json
{
  "total": 3,
  "counts_by_status": {"in_progress": 1, "dispatched": 1, "failed": 1},
  "active": 1,
  "dispatched": 1,
  "failed": 1,
  "dispatch_attempts": 4,
  "last_dispatched_at": "2026-09-19 13:40:02",
  "oldest_in_progress_at": "2026-09-19 13:41:55"
}
```

`dispatch_attempts` counts Devin dispatch attempts including retries, and
`oldest_in_progress_at` surfaces work that is stuck mid-dispatch.

### Upgrading from the idempotency-only schema

On startup the store imports any rows from the previous `deliveries` table into
`remediation_jobs` (`INSERT OR IGNORE`, so it is safe to re-run and never overwrites newer
state), preserving delivery id, status, Devin session id/URL and timestamps. Already-dispatched
deliveries therefore stay non-reclaimable across the upgrade, and previously failed ones stay
retryable. Imported rows have no issue metadata in the old schema, so they get `repository`
`unknown`, issue number `0` and empty title/URL. The `deliveries` table is left in place,
unused, rather than dropped.

## Idempotency

Eligible deliveries are recorded in a SQLite table keyed by the `X-GitHub-Delivery` header, so a
GitHub redelivery never creates a second Devin session:

- The claim is an `INSERT` on that primary key, so exactly one concurrent duplicate wins; the
  losers return `{"status": "in_progress"}` without calling Devin.
- Once dispatched, redeliveries return `{"status": "duplicate"}` with the original session id and
  URL.
- A failed dispatch is marked `failed` and a later redelivery re-claims it, so failures stay
  retryable rather than being suppressed.
- State lives in `DELIVERY_DB_PATH`, so it survives restarts and is shared by processes pointing
  at the same file. In Docker, mount a volume at `/data` to keep it.

An eligible event without an `X-GitHub-Delivery` header is rejected with `400`.

## Docker

Build the image:

```bash
docker build -t devin-remediation-automation .
```

Run it, supplying the secrets at runtime (no secrets are baked into the image):

```bash
docker run --rm -p 8000:8000 \
  -e GITHUB_WEBHOOK_SECRET=replace-me \
  -e DEVIN_API_KEY=replace-me \
  -e DEVIN_ORG_ID=replace-me \
  -v devin-remediation-data:/data \
  devin-remediation-automation
```

Check the health endpoint:

```bash
curl http://localhost:8000/health
# {"status":"healthy"}
```

## Tests

```bash
pytest
```

## Lint

```bash
ruff check .
```

## Layout

```
src/devin_remediation_automation/
    main.py           # FastAPI app factory
    config.py         # environment-backed settings
    remediation_store.py # SQLite remediation job store (idempotency + lifecycle state)
    dependencies.py   # FastAPI providers for the shared HTTP, Devin, and store clients
    devin_client.py   # Devin v3 Organization API client
    remediation.py    # builds the Devin task from the GitHub issue payload
    security.py       # GitHub webhook signature verification
    api/health.py     # GET /health
    api/remediations.py # read-only remediation job + metrics endpoints
    api/webhooks.py   # POST /webhooks/github
tests/
```
