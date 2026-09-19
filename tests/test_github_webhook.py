import hashlib
import hmac
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from devin_remediation_automation.config import Settings, get_settings
from devin_remediation_automation.dependencies import get_devin_client
from devin_remediation_automation.devin_client import DevinAPIError, DevinSession
from devin_remediation_automation.main import create_app

SECRET = "test-secret"
ALLOWED_REPOSITORY = "frances-devin-takehome/superset"


class FakeDevinClient:
    """Stand-in for DevinClient; records calls instead of reaching the Devin API."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def create_session(
        self,
        prompt: str,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
    ) -> DevinSession:
        self.calls.append({"prompt": prompt, "title": title, "tags": tags})
        if self.error is not None:
            raise self.error
        return DevinSession(session_id="devin-abc123", url="https://app.devin.ai/sessions/abc123")


@pytest.fixture
def devin_client() -> FakeDevinClient:
    return FakeDevinClient()


@pytest.fixture
def client(devin_client: FakeDevinClient) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        github_webhook_secret=SECRET,
        devin_api_key="cog_test_key",
        devin_org_id="org-test",
        allowed_repository=ALLOWED_REPOSITORY,
    )
    app.dependency_overrides[get_devin_client] = lambda: devin_client
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def post(client: TestClient, payload: dict[str, Any], event: str = "issues", secret: str = SECRET):
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": f"sha256={signature}",
            "Content-Type": "application/json",
        },
    )


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


def test_eligible_event_creates_devin_session(
    client: TestClient, devin_client: FakeDevinClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="devin_remediation_automation.api.webhooks"):
        response = post(client, labeled_payload())

    assert response.status_code == 200
    assert response.json() == {
        "status": "dispatched",
        "devin_session_id": "devin-abc123",
        "devin_session_url": "https://app.devin.ai/sessions/abc123",
    }

    assert len(devin_client.calls) == 1
    prompt = devin_client.calls[0]["prompt"]
    assert ALLOWED_REPOSITORY in prompt
    assert "#42" in prompt
    assert "Flaky login test" in prompt
    assert f"https://github.com/{ALLOWED_REPOSITORY}/issues/42" in prompt
    assert "Acceptance: test passes 20 runs in a row." in prompt
    assert "pull request" in prompt

    logged = caplog.text
    assert "devin-abc123" in logged
    assert "https://app.devin.ai/sessions/abc123" in logged
    assert "cog_test_key" not in logged


def test_other_repository_does_not_dispatch(
    client: TestClient, devin_client: FakeDevinClient
) -> None:
    response = post(client, labeled_payload(repository="acme/widgets"))

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert devin_client.calls == []


def test_devin_api_failure_is_not_reported_as_dispatched(client: TestClient) -> None:
    app = client.app
    failing = FakeDevinClient(error=DevinAPIError("boom"))
    app.dependency_overrides[get_devin_client] = lambda: failing

    response = post(client, labeled_payload())

    assert response.status_code == 502
    assert response.json()["detail"] == "Failed to create Devin session"


def test_other_label_is_ignored(client: TestClient, devin_client: FakeDevinClient) -> None:
    response = post(client, labeled_payload(label="bug"))

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert devin_client.calls == []


def test_label_prefix_is_not_eligible(client: TestClient, devin_client: FakeDevinClient) -> None:
    response = post(client, labeled_payload(label="devin-remediation-later"))

    assert response.json()["status"] == "ignored"
    assert devin_client.calls == []


def test_other_issue_action_is_ignored(client: TestClient, devin_client: FakeDevinClient) -> None:
    response = post(client, labeled_payload(action="opened"))

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert devin_client.calls == []


def test_other_event_type_is_ignored(client: TestClient, devin_client: FakeDevinClient) -> None:
    response = post(client, {"action": "opened"}, event="pull_request")

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert devin_client.calls == []


def test_invalid_signature_is_rejected(client: TestClient, devin_client: FakeDevinClient) -> None:
    response = post(client, labeled_payload(), secret="wrong-secret")

    assert response.status_code == 401
    assert devin_client.calls == []


def test_missing_signature_is_rejected(client: TestClient, devin_client: FakeDevinClient) -> None:
    response = client.post(
        "/webhooks/github",
        content=json.dumps(labeled_payload()).encode(),
        headers={"X-GitHub-Event": "issues"},
    )

    assert response.status_code == 401
    assert devin_client.calls == []
