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

## Event → Devin session flow

1. GitHub delivers an `issues` event; the `X-Hub-Signature-256` header is verified against
   `GITHUB_WEBHOOK_SECRET`.
2. The event is eligible only when `action` is `labeled`, the added label is exactly
   `devin-remediation`, and the repository full name equals `ALLOWED_REPOSITORY`. Anything else is
   ignored without calling Devin.
3. A task is built from the payload (repository full name, issue number, title, URL, and issue
   body/acceptance criteria) asking Devin to investigate, make the smallest appropriate fix, run
   the relevant validation, and open a pull request. The fix itself is not prescribed.
4. One session is created via the Devin v3 Organization API
   (`POST {DEVIN_API_BASE_URL}/v3/organizations/{DEVIN_ORG_ID}/sessions`). On success the response
   is `{"status": "dispatched", "devin_session_id": ..., "devin_session_url": ...}` and the
   repository, issue, and session identifiers are logged (never credentials).
5. If session creation fails, the endpoint returns `502` and the event is not reported as
   dispatched.

Tests use a mocked Devin API and never create real sessions.

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
    dependencies.py   # FastAPI providers for the shared HTTP and Devin clients
    devin_client.py   # Devin v3 Organization API client
    remediation.py    # builds the Devin task from the GitHub issue payload
    security.py       # GitHub webhook signature verification
    api/health.py     # GET /health
    api/webhooks.py   # POST /webhooks/github
tests/
```
