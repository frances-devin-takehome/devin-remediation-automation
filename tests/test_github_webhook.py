import hashlib
import hmac
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from devin_remediation_automation.config import Settings, get_settings
from devin_remediation_automation.main import create_app

SECRET = "test-secret"


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(github_webhook_secret=SECRET)
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


def labeled_payload(label: str = "devin-remediation", action: str = "labeled") -> dict[str, Any]:
    return {
        "action": action,
        "label": {"name": label},
        "repository": {"full_name": "acme/widgets"},
        "issue": {
            "number": 42,
            "title": "Flaky login test",
            "html_url": "https://github.com/acme/widgets/issues/42",
        },
    }


def test_eligible_event_is_accepted_and_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="devin_remediation_automation.api.webhooks"):
        response = post(client, labeled_payload())

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    logged = caplog.text
    assert "acme/widgets" in logged
    assert "42" in logged
    assert "Flaky login test" in logged
    assert "https://github.com/acme/widgets/issues/42" in logged


def test_other_label_is_ignored(client: TestClient) -> None:
    response = post(client, labeled_payload(label="bug"))

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}


def test_label_prefix_is_not_eligible(client: TestClient) -> None:
    response = post(client, labeled_payload(label="devin-remediation-later"))

    assert response.json() == {"status": "ignored"}


def test_other_issue_action_is_ignored(client: TestClient) -> None:
    response = post(client, labeled_payload(action="opened"))

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}


def test_other_event_type_is_ignored(client: TestClient) -> None:
    response = post(client, {"action": "opened"}, event="pull_request")

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}


def test_invalid_signature_is_rejected(client: TestClient) -> None:
    response = post(client, labeled_payload(), secret="wrong-secret")

    assert response.status_code == 401


def test_missing_signature_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/webhooks/github",
        content=json.dumps(labeled_payload()).encode(),
        headers={"X-GitHub-Event": "issues"},
    )

    assert response.status_code == 401
