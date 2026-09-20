#!/usr/bin/env python3
"""Send a signed synthetic GitHub webhook to a running devin-remediation-automation instance.

Usage:
    export GITHUB_WEBHOOK_SECRET=...   # same value the service was started with
    python scripts/simulate_webhook.py issues
    python scripts/simulate_webhook.py pull_request
    python scripts/simulate_webhook.py workflow_run --status in_progress
    python scripts/simulate_webhook.py workflow_run --conclusion failure

The three events chain through one remediation: `issues` claims a job under
--remediation-id, `pull_request` carries `Remediation-ID: <that id>` in its body, and
`workflow_run` references the same --pr-number. Payloads mirror tests/helpers.py.

Only the service is contacted; whether it then calls the Devin API depends on the
credentials the service itself was started with.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_REPOSITORY = "frances-devin-takehome/superset"
DEFAULT_REMEDIATION_ID = "sim-delivery-1"
DEFAULT_PR_NUMBER = 7
EVENTS = ("issues", "pull_request", "workflow_run")


def issues_payload(repository: str) -> dict[str, Any]:
    return {
        "action": "labeled",
        "label": {"name": "devin-remediation"},
        "repository": {"full_name": repository},
        "issue": {
            "number": 42,
            "title": "Flaky login test",
            "html_url": f"https://github.com/{repository}/issues/42",
            "body": "Login test fails intermittently.\n\nAcceptance: test passes 20 runs in a row.",
        },
    }


def pull_request_payload(repository: str, remediation_id: str, pr_number: int) -> dict[str, Any]:
    return {
        "action": "opened",
        "repository": {"full_name": repository},
        "pull_request": {
            "number": pr_number,
            "html_url": f"https://github.com/{repository}/pull/{pr_number}",
            "body": f"Fixes the flaky test.\n\nRemediation-ID: {remediation_id}\n",
            "created_at": "2026-09-19T12:00:00Z",
        },
    }


def workflow_run_payload(
    repository: str, pr_number: int, status: str, conclusion: str, run_id: int
) -> dict[str, Any]:
    completed = status == "completed"
    return {
        "action": "completed" if completed else "in_progress",
        "repository": {"full_name": repository},
        "workflow_run": {
            "id": run_id,
            "name": "Remediation validation",
            "status": status,
            "conclusion": conclusion if completed else None,
            "html_url": f"https://github.com/{repository}/actions/runs/{run_id}",
            "updated_at": "2026-09-19T12:30:00Z",
            "pull_requests": [{"number": pr_number}],
        },
    }


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.event == "issues":
        return issues_payload(args.repository)
    if args.event == "pull_request":
        return pull_request_payload(args.repository, args.remediation_id, args.pr_number)
    return workflow_run_payload(
        args.repository, args.pr_number, args.status, args.conclusion, args.run_id
    )


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("event", choices=EVENTS)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"default: {DEFAULT_BASE_URL}")
    parser.add_argument(
        "--repository",
        default=os.environ.get("ALLOWED_REPOSITORY", DEFAULT_REPOSITORY),
        help="repository full_name in the payload (default: $ALLOWED_REPOSITORY or %(default)s)",
    )
    parser.add_argument(
        "--remediation-id",
        default=DEFAULT_REMEDIATION_ID,
        help="X-GitHub-Delivery of the `issues` event and the Remediation-ID in the PR body",
    )
    parser.add_argument(
        "--delivery-id",
        default=None,
        help="X-GitHub-Delivery header (default: --remediation-id for issues, random otherwise)",
    )
    parser.add_argument("--pr-number", type=int, default=DEFAULT_PR_NUMBER)
    parser.add_argument("--run-id", type=int, default=555, help="workflow_run id")
    parser.add_argument(
        "--status",
        choices=("queued", "in_progress", "completed"),
        default="completed",
        help="workflow_run status",
    )
    parser.add_argument(
        "--conclusion", default="success", help="workflow_run conclusion when completed"
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if not secret:
        print("GITHUB_WEBHOOK_SECRET is not set", file=sys.stderr)
        return 2

    delivery_id = args.delivery_id or (
        args.remediation_id if args.event == "issues" else f"sim-{uuid.uuid4()}"
    )
    body = json.dumps(build_payload(args)).encode()
    request = urllib.request.Request(
        f"{args.base_url.rstrip('/')}/webhooks/github",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": args.event,
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": sign(secret, body),
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except urllib.error.URLError as exc:
        print(f"Could not reach {request.full_url}: {exc.reason}", file=sys.stderr)
        return 1

    print(f"{args.event} delivery={delivery_id} -> HTTP {status}")
    try:
        print(json.dumps(json.loads(raw), indent=2))
    except ValueError:
        print(raw.decode(errors="replace"))
    return 0 if status < 400 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
