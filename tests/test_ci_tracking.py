import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devin_remediation_automation.remediation import RemediationRequest
from devin_remediation_automation.remediation_store import RemediationStatus, RemediationStore
from helpers import (
    FakeDevinClient,
    build_app,
    labeled_payload,
    pull_request_payload,
    signed_request,
    workflow_run_payload,
)

# Schema of the release that introduced pull-request correlation, before CI tracking.
PRE_CI_SCHEMA = """
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
    dispatched_at TEXT,
    pr_number INTEGER,
    pr_url TEXT,
    pr_created_at TEXT
)
"""

REQUEST = RemediationRequest(
    repository_full_name="frances-devin-takehome/superset",
    issue_number=42,
    issue_title="Flaky login test",
    issue_url="https://github.com/frances-devin-takehome/superset/issues/42",
    issue_body="",
)


def post(client: TestClient, payload: dict, *, event: str, delivery_id: str = "d") -> object:
    body, headers = signed_request(payload, event=event, delivery_id=delivery_id)
    return client.post("/webhooks/github", content=body, headers=headers)


def correlated_job(client: TestClient) -> None:
    assert (
        post(client, labeled_payload(), event="issues", delivery_id="delivery-1").json()["status"]
        == "dispatched"
    )
    assert (
        post(client, pull_request_payload(), event="pull_request", delivery_id="pr-1").json()[
            "status"
        ]
        == "pr_correlated"
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = build_app(FakeDevinClient(), str(tmp_path / "deliveries.db"))
    with TestClient(app) as test_client:
        yield test_client


def job(client: TestClient) -> dict:
    return client.get("/remediations/delivery-1").json()


def test_running_workflow_moves_job_to_ci_running(client: TestClient) -> None:
    correlated_job(client)

    response = post(client, workflow_run_payload(status="in_progress"), event="workflow_run")

    assert response.status_code == 200
    assert response.json()["status"] == "ci_running"
    recorded = job(client)
    assert recorded["status"] == "ci_running"
    assert recorded["ci_run_id"] == 555
    assert recorded["ci_run_url"].endswith("/actions/runs/555")
    assert recorded["ci_conclusion"] is None
    assert recorded["ci_completed_at"] is None


def test_successful_workflow_marks_job_succeeded(client: TestClient) -> None:
    correlated_job(client)
    post(client, workflow_run_payload(status="in_progress"), event="workflow_run")

    response = post(client, workflow_run_payload(), event="workflow_run")

    assert response.json()["status"] == "succeeded"
    recorded = job(client)
    assert recorded["status"] == "succeeded"
    assert recorded["ci_conclusion"] == "success"
    assert recorded["ci_completed_at"] == "2026-09-19T12:30:00Z"


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "startup_failure", "cancelled"])
def test_failure_like_conclusions_mark_job_failed(client: TestClient, conclusion: str) -> None:
    correlated_job(client)

    response = post(client, workflow_run_payload(conclusion=conclusion), event="workflow_run")

    assert response.json()["status"] == "failed"
    recorded = job(client)
    assert recorded["status"] == "failed"
    assert recorded["ci_conclusion"] == conclusion


def test_ci_failure_is_not_redispatched_by_an_issue_redelivery(client: TestClient) -> None:
    correlated_job(client)
    post(client, workflow_run_payload(conclusion="failure"), event="workflow_run")

    redelivery = post(client, labeled_payload(), event="issues", delivery_id="delivery-1")

    assert redelivery.json()["status"] == "in_progress"
    assert job(client)["status"] == "failed"


def test_redelivered_workflow_events_are_idempotent(client: TestClient) -> None:
    correlated_job(client)
    payload = workflow_run_payload()

    first = post(client, payload, event="workflow_run")
    second = post(client, payload, event="workflow_run")
    late_running = post(client, workflow_run_payload(status="in_progress"), event="workflow_run")

    assert first.json()["status"] == "succeeded"
    assert second.json()["status"] == "succeeded"
    # A late in-progress event must not drag a finished remediation backwards.
    assert late_running.json()["status"] == "succeeded"
    assert client.get("/remediations").json()["count"] == 1


def test_successful_rerun_replaces_an_earlier_failure(client: TestClient) -> None:
    correlated_job(client)
    post(client, workflow_run_payload(conclusion="failure"), event="workflow_run")

    rerun = post(client, workflow_run_payload(run_id=556), event="workflow_run")

    assert rerun.json()["status"] == "succeeded"
    recorded = job(client)
    assert recorded["status"] == "succeeded"
    assert recorded["ci_run_id"] == 556
    assert recorded["ci_conclusion"] == "success"


def test_failed_rerun_replaces_an_earlier_success(client: TestClient) -> None:
    correlated_job(client)
    post(client, workflow_run_payload(), event="workflow_run")

    rerun = post(
        client, workflow_run_payload(run_id=556, conclusion="failure"), event="workflow_run"
    )

    assert rerun.json()["status"] == "failed"
    recorded = job(client)
    assert recorded["status"] == "failed"
    assert recorded["ci_run_id"] == 556
    assert recorded["ci_conclusion"] == "failure"


def test_rerun_in_progress_event_never_regresses_a_completed_result(client: TestClient) -> None:
    correlated_job(client)
    post(client, workflow_run_payload(), event="workflow_run")

    queued_rerun = post(
        client, workflow_run_payload(run_id=556, status="in_progress"), event="workflow_run"
    )

    assert queued_rerun.json()["status"] == "succeeded"
    recorded = job(client)
    assert recorded["status"] == "succeeded"
    assert recorded["ci_run_id"] == 555
    assert recorded["ci_conclusion"] == "success"
    assert recorded["ci_completed_at"] == "2026-09-19T12:30:00Z"


def test_unrelated_workflow_is_ignored(client: TestClient) -> None:
    correlated_job(client)

    response = post(client, workflow_run_payload(name="Lint"), event="workflow_run")

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert job(client)["status"] == "pr_created"


def test_workflow_from_other_repository_is_ignored(client: TestClient) -> None:
    correlated_job(client)

    response = post(client, workflow_run_payload(repository="acme/widgets"), event="workflow_run")

    assert response.json()["status"] == "ignored"
    assert job(client)["status"] == "pr_created"


def test_workflow_for_unknown_pull_request_is_ignored(client: TestClient) -> None:
    correlated_job(client)

    response = post(client, workflow_run_payload(pr_number=404), event="workflow_run")

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert job(client)["status"] == "pr_created"


def test_workflow_without_pull_request_is_ignored(client: TestClient) -> None:
    correlated_job(client)

    response = post(client, workflow_run_payload(pr_number=None), event="workflow_run")

    assert response.json()["status"] == "ignored"
    assert job(client)["status"] == "pr_created"


def test_workflow_event_never_creates_a_job(client: TestClient) -> None:
    post(client, workflow_run_payload(), event="workflow_run")

    assert client.get("/remediations").json()["count"] == 0


def test_workflow_event_with_bad_signature_is_rejected(client: TestClient) -> None:
    body, headers = signed_request(
        workflow_run_payload(), event="workflow_run", secret="wrong", delivery_id="wf-1"
    )

    assert client.post("/webhooks/github", content=body, headers=headers).status_code == 401


def test_ci_outcomes_are_visible_in_metrics(client: TestClient) -> None:
    correlated_job(client)
    post(client, workflow_run_payload(), event="workflow_run")

    metrics = client.get("/remediations/metrics").json()

    assert metrics["succeeded"] == 1
    assert metrics["pr_created"] == 0
    assert metrics["counts_by_status"]["succeeded"] == 1
    assert client.get("/remediations", params={"status": "succeeded"}).json()["count"] == 1


def test_existing_database_without_ci_columns_is_upgraded(tmp_path: Path) -> None:
    database_path = tmp_path / "deliveries.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(PRE_CI_SCHEMA)
        connection.execute(
            "INSERT INTO remediation_jobs (delivery_id, repository, issue_number, issue_title, "
            "issue_url, status, devin_session_id, pr_number) VALUES "
            "('old', 'frances-devin-takehome/superset', 42, 't', 'u', 'pr_created', 'devin-1', 7)"
        )

    store = RemediationStore(database_path)
    existing = store.get("old")
    assert existing is not None
    assert existing.ci_run_id is None

    updated = store.record_workflow_run(
        7,
        run_id=555,
        run_url="https://github.com/o/r/actions/runs/555",
        status=RemediationStatus.SUCCEEDED,
        conclusion="success",
        completed_at="2026-09-19T12:30:00Z",
    )

    assert updated is not None
    assert updated.status is RemediationStatus.SUCCEEDED
    assert updated.ci_run_id == 555
    assert not store.claim("old", REQUEST).acquired
