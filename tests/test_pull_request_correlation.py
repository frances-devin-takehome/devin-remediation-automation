import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devin_remediation_automation.remediation import RemediationRequest, extract_remediation_id
from devin_remediation_automation.remediation_store import RemediationStatus, RemediationStore
from helpers import (
    FakeDevinClient,
    build_app,
    labeled_payload,
    pull_request_payload,
    signed_request,
)

# Schema of the release that introduced remediation_jobs, before pull-request correlation.
PRE_CORRELATION_SCHEMA = """
CREATE TABLE remediation_jobs (
    delivery_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    issue_title TEXT NOT NULL,
    issue_url TEXT NOT NULL,
    status TEXT NOT NULL,
    devin_session_id TEXT,
    devin_session_url TEXT,
    attempts INTEGER NOT NULL DEFAULT 1,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    dispatched_at TEXT
)
"""

REQUEST = RemediationRequest(
    repository_full_name="frances-devin-takehome/superset",
    issue_number=42,
    issue_title="Flaky login test",
    issue_url="https://github.com/frances-devin-takehome/superset/issues/42",
    issue_body="",
)


def post(client: TestClient, payload: dict, *, event: str, delivery_id: str) -> object:
    body, headers = signed_request(payload, event=event, delivery_id=delivery_id)
    return client.post("/webhooks/github", content=body, headers=headers)


def dispatch_issue(client: TestClient, delivery_id: str = "delivery-1") -> None:
    response = post(client, labeled_payload(), event="issues", delivery_id=delivery_id)
    assert response.json()["status"] == "dispatched"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = build_app(FakeDevinClient(), str(tmp_path / "deliveries.db"))
    with TestClient(app) as test_client:
        yield test_client


def test_opened_pull_request_is_correlated(client: TestClient) -> None:
    dispatch_issue(client)

    response = post(client, pull_request_payload(), event="pull_request", delivery_id="pr-1")

    assert response.status_code == 200
    assert response.json()["status"] == "pr_correlated"
    job = client.get("/remediations/delivery-1").json()
    assert job["status"] == RemediationStatus.PR_CREATED.value
    assert job["pr_number"] == 7
    assert job["pr_url"].endswith("/pull/7")
    assert job["pr_created_at"] == "2026-09-19T12:00:00Z"
    assert job["devin_session_id"] == "devin-abc1"


def test_correlation_is_visible_in_metrics_and_listing(client: TestClient) -> None:
    dispatch_issue(client)
    post(client, pull_request_payload(), event="pull_request", delivery_id="pr-1")

    metrics = client.get("/remediations/metrics").json()
    listed = client.get("/remediations", params={"status": "pr_created"}).json()

    assert metrics["pr_created"] == 1
    assert metrics["dispatched"] == 0
    assert listed["count"] == 1
    assert listed["jobs"][0]["pr_number"] == 7


def test_redelivered_pull_request_event_is_idempotent(client: TestClient) -> None:
    dispatch_issue(client)
    payload = pull_request_payload()

    first = post(client, payload, event="pull_request", delivery_id="pr-1")
    second = post(client, payload, event="pull_request", delivery_id="pr-1")
    third = post(client, payload, event="pull_request", delivery_id="pr-2")

    assert [r.json()["status"] for r in (first, second, third)] == ["pr_correlated"] * 3
    jobs = client.get("/remediations").json()
    assert jobs["count"] == 1
    assert jobs["jobs"][0]["pr_number"] == 7


def test_second_pull_request_does_not_replace_the_first(client: TestClient) -> None:
    dispatch_issue(client)
    post(client, pull_request_payload(), event="pull_request", delivery_id="pr-1")

    response = post(
        client, pull_request_payload(number=9), event="pull_request", delivery_id="pr-2"
    )

    assert response.json()["status"] == "duplicate"
    assert client.get("/remediations/delivery-1").json()["pr_number"] == 7


def test_pull_request_without_marker_is_ignored(client: TestClient) -> None:
    dispatch_issue(client)

    response = post(
        client, pull_request_payload(body="No marker here"), event="pull_request", delivery_id="x"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert client.get("/remediations/delivery-1").json()["status"] == "dispatched"


def test_pull_request_with_unknown_marker_is_ignored(client: TestClient) -> None:
    dispatch_issue(client)

    response = post(
        client,
        pull_request_payload(body="Remediation-ID: delivery-missing"),
        event="pull_request",
        delivery_id="x",
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert client.get("/remediations/delivery-1").json()["status"] == "dispatched"


def test_pull_request_from_other_repository_is_ignored(client: TestClient) -> None:
    dispatch_issue(client)

    response = post(
        client,
        pull_request_payload(repository="acme/widgets"),
        event="pull_request",
        delivery_id="x",
    )

    assert response.json()["status"] == "ignored"
    assert client.get("/remediations/delivery-1").json()["status"] == "dispatched"


def test_non_opened_pull_request_action_is_ignored(client: TestClient) -> None:
    dispatch_issue(client)

    response = post(
        client, pull_request_payload(action="closed"), event="pull_request", delivery_id="x"
    )

    assert response.json()["status"] == "ignored"
    assert client.get("/remediations/delivery-1").json()["status"] == "dispatched"


def test_pull_request_event_with_bad_signature_is_rejected(client: TestClient) -> None:
    body, headers = signed_request(
        pull_request_payload(), event="pull_request", secret="wrong", delivery_id="pr-1"
    )

    response = client.post("/webhooks/github", content=body, headers=headers)

    assert response.status_code == 401


def test_pull_request_event_never_creates_a_job(client: TestClient) -> None:
    post(client, pull_request_payload(), event="pull_request", delivery_id="pr-1")

    assert client.get("/remediations").json()["count"] == 0


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Remediation-ID: abc-123", "abc-123"),
        ("intro\n\nremediation-id:   abc-123  \noutro", "abc-123"),
        ("Remediation-ID: abc-123 and trailing prose", None),
        ("nothing here", None),
        (None, None),
    ],
)
def test_marker_extraction(body: str | None, expected: str | None) -> None:
    assert extract_remediation_id(body) == expected


def test_existing_database_without_pr_columns_is_upgraded(tmp_path: Path) -> None:
    database_path = tmp_path / "deliveries.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(PRE_CORRELATION_SCHEMA)
        connection.execute(
            "INSERT INTO remediation_jobs (delivery_id, repository, issue_number, issue_title, "
            "issue_url, status, devin_session_id) VALUES "
            "('old-dispatched', 'frances-devin-takehome/superset', 7, 't', 'u', 'dispatched', "
            "'devin-old')"
        )

    store = RemediationStore(database_path)
    existing = store.get("old-dispatched")
    assert existing is not None
    assert existing.pr_number is None

    correlated = store.record_pull_request(
        "old-dispatched", 7, "https://github.com/o/r/pull/7", "2026-09-19T12:00:00Z"
    )

    assert correlated is not None
    assert correlated.status is RemediationStatus.PR_CREATED
    assert correlated.pr_number == 7
    assert not store.claim("old-dispatched", REQUEST).acquired
