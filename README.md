# devin-remediation-automation

Python service that will orchestrate automated engineering remediation using GitHub webhooks and
the Devin API. This repository currently contains only the service skeleton.

## Requirements

- Python 3.12

## Configuration

Configuration is read from the process environment (see `.env.example` for the full list):

| Variable | Required | Description |
| --- | --- | --- |
| `GITHUB_WEBHOOK_SECRET` | yes | Shared secret configured on the GitHub webhook, used to verify the `X-Hub-Signature-256` header. |

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
- `POST /webhooks/github` — GitHub webhook receiver. Signature-verified; an `issues` event with
  action `labeled` and label exactly `devin-remediation` is logged as remediation-eligible
  (`{"status": "accepted"}`). Other valid events return `{"status": "ignored"}`, and an invalid or
  missing signature returns `401`.

## Docker

Build the image:

```bash
docker build -t devin-remediation-automation .
```

Run it, supplying the webhook secret at runtime (no secrets are baked into the image):

```bash
docker run --rm -p 8000:8000 -e GITHUB_WEBHOOK_SECRET=replace-me devin-remediation-automation
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
    main.py          # FastAPI app factory
    config.py        # environment-backed settings
    security.py      # GitHub webhook signature verification
    api/health.py    # GET /health
    api/webhooks.py  # POST /webhooks/github
tests/
```
