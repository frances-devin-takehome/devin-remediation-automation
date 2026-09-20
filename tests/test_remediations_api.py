from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from devin_remediation_automation.devin_client import DevinAPIError
from helpers import FakeDevinClient, build_app, labeled_payload, signed_request


def post(client: TestClient, delivery_id: str, issue_number: int = 42) -> httpx.Response:
    payload = labeled_payload()
    payload["issue"]["number"] = issue_number
    body, headers = signed_request(payload, delivery_id=delivery_id)
    return client.post("/webhooks/github", content=body, headers=headers)


def test_lists_jobs_with_issue_and_session_details(tmp_path: Path) -> None:
    app = build_app(FakeDevinClient(), str(tmp_path / "deliveries.db"))

    with TestClient(app) as client:
        dispatched = post(client, "delivery-1")
        listed = client.get("/remediations").json()

    job = listed["jobs"][0]
    assert listed["count"] == 1
    assert job["delivery_id"] == "delivery-1"
    assert job["repository"] == "frances-devin-takehome/superset"
    assert job["issue_number"] == 42
    assert job["issue_title"] == "Flaky login test"
    assert job["issue_url"].endswith("/issues/42")
    assert job["status"] == "dispatched"
    assert job["devin_session_id"] == dispatched.json()["devin_session_id"]
    assert job["devin_session_url"] == dispatched.json()["devin_session_url"]
    assert job["attempts"] == 1
    assert job["dispatched_at"] is not None
    assert job["last_error"] is None


def test_failed_job_records_failure_information(tmp_path: Path) -> None:
    app = build_app(
        FakeDevinClient(error=DevinAPIError("Devin API request failed: 500")),
        str(tmp_path / "deliveries.db"),
    )

    with TestClient(app) as client:
        assert post(client, "delivery-1").status_code == 502
        job = client.get("/remediations/delivery-1").json()

    assert job["status"] == "failed"
    assert job["last_error"] == "Devin API request failed: 500"
    assert job["devin_session_id"] is None
    assert job["dispatched_at"] is None


def test_unknown_delivery_returns_404(tmp_path: Path) -> None:
    app = build_app(FakeDevinClient(), str(tmp_path / "deliveries.db"))

    with TestClient(app) as client:
        assert client.get("/remediations/nope").status_code == 404


def test_jobs_can_be_filtered_by_status(tmp_path: Path) -> None:
    database_path = str(tmp_path / "deliveries.db")

    with TestClient(build_app(FakeDevinClient(), database_path)) as client:
        post(client, "delivery-1")
    with TestClient(
        build_app(FakeDevinClient(error=DevinAPIError("boom")), database_path)
    ) as client:
        post(client, "delivery-2", issue_number=43)
        dispatched = client.get("/remediations", params={"status": "dispatched"}).json()
        failed = client.get("/remediations", params={"status": "failed"}).json()
        limited = client.get("/remediations", params={"limit": 1}).json()

    assert [job["delivery_id"] for job in dispatched["jobs"]] == ["delivery-1"]
    assert [job["delivery_id"] for job in failed["jobs"]] == ["delivery-2"]
    assert limited["count"] == 1


def test_metrics_aggregate_by_status(tmp_path: Path) -> None:
    database_path = str(tmp_path / "deliveries.db")

    with TestClient(build_app(FakeDevinClient(), database_path)) as client:
        post(client, "delivery-1")
    with TestClient(
        build_app(FakeDevinClient(error=DevinAPIError("boom")), database_path)
    ) as client:
        post(client, "delivery-2", issue_number=43)
        post(client, "delivery-2", issue_number=43)  # retry of the failed delivery
        metrics = client.get("/remediations/metrics").json()

    assert metrics["total"] == 2
    assert metrics["counts_by_status"] == {
        "in_progress": 0,
        "dispatched": 1,
        "pr_created": 0,
        "ci_running": 0,
        "succeeded": 0,
        "failed": 1,
    }
    assert metrics["active"] == 0
    assert metrics["dispatched"] == 1
    assert metrics["failed"] == 1
    # The failed delivery was attempted twice; dispatched counts sessions created, not fixes.
    assert metrics["dispatch_attempts"] == 3
    assert metrics["last_dispatched_at"] is not None
    assert metrics["oldest_in_progress_at"] is None


def test_metrics_are_empty_without_jobs(tmp_path: Path) -> None:
    app = build_app(FakeDevinClient(), str(tmp_path / "deliveries.db"))

    with TestClient(app) as client:
        metrics = client.get("/remediations/metrics").json()

    assert metrics["total"] == 0
    assert metrics["counts_by_status"] == {
        "in_progress": 0,
        "dispatched": 0,
        "pr_created": 0,
        "ci_running": 0,
        "succeeded": 0,
        "failed": 0,
    }
    assert metrics["dispatch_attempts"] == 0
    assert metrics["last_dispatched_at"] is None
