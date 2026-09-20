import asyncio
import hashlib
import hmac
import itertools
import json
from typing import Any

from fastapi import FastAPI

from devin_remediation_automation.config import Settings, get_settings
from devin_remediation_automation.dependencies import get_devin_client
from devin_remediation_automation.devin_client import DevinSession
from devin_remediation_automation.main import create_app

SECRET = "test-secret"
ALLOWED_REPOSITORY = "frances-devin-takehome/superset"


class FakeDevinClient:
    """Stand-in for DevinClient; records calls instead of reaching the Devin API."""

    def __init__(self, error: Exception | None = None, delay: float = 0.0) -> None:
        self.error = error
        self.delay = delay
        self.calls: list[dict[str, Any]] = []
        self._ids = itertools.count(1)

    async def create_session(
        self,
        prompt: str,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
    ) -> DevinSession:
        self.calls.append({"prompt": prompt, "title": title, "tags": tags})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        suffix = next(self._ids)
        return DevinSession(
            session_id=f"devin-abc{suffix}",
            url=f"https://app.devin.ai/sessions/abc{suffix}",
        )


def build_app(devin_client: FakeDevinClient, delivery_db_path: str) -> FastAPI:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        github_webhook_secret=SECRET,
        devin_api_key="cog_test_key",
        devin_org_id="org-test",
        allowed_repository=ALLOWED_REPOSITORY,
        delivery_db_path=delivery_db_path,
    )
    app.dependency_overrides[get_devin_client] = lambda: devin_client
    return app


def signed_request(
    payload: dict[str, Any],
    *,
    event: str = "issues",
    secret: str = SECRET,
    delivery_id: str | None = "delivery-1",
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": f"sha256={signature}",
        "Content-Type": "application/json",
    }
    if delivery_id is not None:
        headers["X-GitHub-Delivery"] = delivery_id
    return body, headers


def pull_request_payload(
    *,
    body: str | None = "Fixes the flaky test.\n\nRemediation-ID: delivery-1\n",
    number: int = 7,
    action: str = "opened",
    repository: str = ALLOWED_REPOSITORY,
) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": repository},
        "pull_request": {
            "number": number,
            "html_url": f"https://github.com/{repository}/pull/{number}",
            "body": body,
            "created_at": "2026-09-19T12:00:00Z",
        },
    }


def workflow_run_payload(
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    pr_number: int | None = 7,
    run_id: int = 555,
    name: str = "Remediation validation",
    repository: str = ALLOWED_REPOSITORY,
) -> dict[str, Any]:
    pull_requests = [] if pr_number is None else [{"number": pr_number}]
    return {
        "action": "completed" if status == "completed" else "in_progress",
        "repository": {"full_name": repository},
        "workflow_run": {
            "id": run_id,
            "name": name,
            "status": status,
            "conclusion": conclusion if status == "completed" else None,
            "html_url": f"https://github.com/{repository}/actions/runs/{run_id}",
            "updated_at": "2026-09-19T12:30:00Z",
            "pull_requests": pull_requests,
        },
    }


def labeled_payload(
    label: str = "devin-remediation",
    action: str = "labeled",
    repository: str = ALLOWED_REPOSITORY,
) -> dict[str, Any]:
    return {
        "action": action,
        "label": {"name": label},
        "repository": {"full_name": repository},
        "issue": {
            "number": 42,
            "title": "Flaky login test",
            "html_url": f"https://github.com/{repository}/issues/42",
            "body": "Login test fails intermittently.\n\nAcceptance: test passes 20 runs in a row.",
        },
    }
