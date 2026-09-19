# devin-remediation-automation

Python service that will orchestrate automated engineering remediation using GitHub webhooks and
the Devin API. This repository currently contains only the service skeleton.

## Requirements

- Python 3.12

## Local development

Create a virtual environment and install the project with development dependencies:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run the service:

```bash
uvicorn devin_remediation_automation.main:app --reload --port 8000
```

Check the health endpoint:

```bash
curl http://localhost:8000/health
# {"status":"healthy"}
```

Interactive API docs are available at http://localhost:8000/docs.

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
    api/health.py    # GET /health
tests/
```
