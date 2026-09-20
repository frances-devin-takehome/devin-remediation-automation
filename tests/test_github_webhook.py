import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from devin_remediation_automation.dependencies import get_devin_client
from devin_remediation_automation.devin_client import DevinAPIError
from helpers import (
    ALLOWED_REPOSITORY,
    SECRET,
    FakeDevinClient,
    build_app,
    labeled_payload,
    signed_request,
)


@pytest.fixture
def devin_client() -> FakeDevinClient:
    return FakeDevinClient()


@pytest.fixture
def client(devin_client: FakeDevinClient, tmp_path: Path) -> Iterator[TestClient]:
    app = build_app(devin_client, str(tmp_path / "deliveries.db"))
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def post(
    client: TestClient,
    payload: dict[str, Any],
    event: str = "issues",
    secret: str = SECRET,
    delivery_id: str | None = "delivery-1",
):
    body, headers = signed_request(payload, event=event, secret=secret, delivery_id=delivery_id)
    return client.post("/webhooks/github", content=body, headers=headers)


def test_eligible_event_creates_devin_session(
    client: TestClient, devin_client: FakeDevinClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="devin_remediation_automation.api.webhooks"):
        response = post(client, labeled_payload())

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "dispatched"
    assert payload["devin_session_id"] == "devin-abc1"
    assert payload["devin_session_url"] == "https://app.devin.ai/sessions/abc1"

    assert len(devin_client.calls) == 1
    prompt = devin_client.calls[0]["prompt"]
    assert ALLOWED_REPOSITORY in prompt
    assert "#42" in prompt
    assert "Flaky login test" in prompt
    assert f"https://github.com/{ALLOWED_REPOSITORY}/issues/42" in prompt
    assert "Acceptance: test passes 20 runs in a row." in prompt
    assert "pull request" in prompt
    assert "Remediation-ID: delivery-1" in prompt

    logged = caplog.text
    assert "devin-abc1" in logged
    assert "https://app.devin.ai/sessions/abc1" in logged
    assert "cog_test_key" not in logged


def test_other_repository_does_not_dispatch(
    client: TestClient, devin_client: FakeDevinClient
) -> None:
    response = post(client, labeled_payload(repository="acme/widgets"))

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert devin_client.calls == []


def test_missing_delivery_header_is_rejected(
    client: TestClient, devin_client: FakeDevinClient
) -> None:
    response = post(client, labeled_payload(), delivery_id=None)

    assert response.status_code == 400
    assert devin_client.calls == []


def test_devin_api_failure_is_not_reported_as_dispatched(client: TestClient) -> None:
    failing = FakeDevinClient(error=DevinAPIError("boom"))
    client.app.dependency_overrides[get_devin_client] = lambda: failing

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
    response = post(client, {"ref": "refs/heads/main"}, event="push")

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
        headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "delivery-1"},
    )

    assert response.status_code == 401
    assert devin_client.calls == []
