import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from devin_remediation_automation.dependencies import get_devin_client
from devin_remediation_automation.devin_client import DevinAPIError
from devin_remediation_automation.remediation import RemediationRequest
from devin_remediation_automation.remediation_store import RemediationStatus, RemediationStore
from helpers import FakeDevinClient, build_app, labeled_payload, signed_request

REQUEST = RemediationRequest(
    repository_full_name="frances-devin-takehome/superset",
    issue_number=42,
    issue_title="Flaky login test",
    issue_url="https://github.com/frances-devin-takehome/superset/issues/42",
    issue_body="body",
)


def post(client: TestClient, delivery_id: str = "delivery-1") -> httpx.Response:
    body, headers = signed_request(labeled_payload(), delivery_id=delivery_id)
    return client.post("/webhooks/github", content=body, headers=headers)


def stored_status(database_path: Path, delivery_id: str) -> str:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT status FROM remediation_jobs WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
    return row[0]


def test_redelivery_does_not_create_a_second_session(tmp_path: Path) -> None:
    devin_client = FakeDevinClient()
    app = build_app(devin_client, str(tmp_path / "deliveries.db"))

    with TestClient(app) as client:
        first = post(client)
        second = post(client)

    assert first.json()["status"] == "dispatched"
    assert second.json() == {
        "status": "duplicate",
        "devin_session_id": first.json()["devin_session_id"],
        "devin_session_url": first.json()["devin_session_url"],
    }
    assert len(devin_client.calls) == 1


def test_distinct_deliveries_each_dispatch(tmp_path: Path) -> None:
    devin_client = FakeDevinClient()
    app = build_app(devin_client, str(tmp_path / "deliveries.db"))

    with TestClient(app) as client:
        first = post(client, delivery_id="delivery-1")
        second = post(client, delivery_id="delivery-2")

    assert first.json()["status"] == "dispatched"
    assert second.json()["status"] == "dispatched"
    assert first.json()["devin_session_id"] != second.json()["devin_session_id"]
    assert len(devin_client.calls) == 2


def test_idempotency_survives_a_new_service_instance(tmp_path: Path) -> None:
    database_path = str(tmp_path / "deliveries.db")
    first_client = FakeDevinClient()
    second_client = FakeDevinClient()

    with TestClient(build_app(first_client, database_path)) as client:
        first = post(client)

    # A restarted process reads the same SQLite file and must not redispatch.
    with TestClient(build_app(second_client, database_path)) as client:
        second = post(client)

    assert first.json()["status"] == "dispatched"
    assert second.json()["status"] == "duplicate"
    assert second.json()["devin_session_id"] == first.json()["devin_session_id"]
    assert len(first_client.calls) == 1
    assert second_client.calls == []


def test_failed_dispatch_is_retryable(tmp_path: Path) -> None:
    database_path = tmp_path / "deliveries.db"
    failing = FakeDevinClient(error=DevinAPIError("boom"))
    succeeding = FakeDevinClient()
    app = build_app(failing, str(database_path))

    with TestClient(app) as client:
        failure = post(client)
        assert failure.status_code == 502
        assert stored_status(database_path, "delivery-1") == RemediationStatus.FAILED.value

        app.dependency_overrides[get_devin_client] = lambda: succeeding
        retry = post(client)

    assert retry.status_code == 200
    assert retry.json()["status"] == "dispatched"
    assert len(succeeding.calls) == 1
    assert stored_status(database_path, "delivery-1") == RemediationStatus.DISPATCHED.value


async def test_concurrent_duplicate_deliveries_dispatch_once(tmp_path: Path) -> None:
    devin_client = FakeDevinClient(delay=0.05)
    app = build_app(devin_client, str(tmp_path / "deliveries.db"))
    body, headers = signed_request(labeled_payload(), delivery_id="delivery-1")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(
            *(client.post("/webhooks/github", content=body, headers=headers) for _ in range(5))
        )

    statuses = sorted(response.json()["status"] for response in responses)
    assert statuses == ["dispatched", "in_progress", "in_progress", "in_progress", "in_progress"]
    assert len(devin_client.calls) == 1


def test_store_claim_is_exclusive_across_threads(tmp_path: Path) -> None:
    store = RemediationStore(tmp_path / "deliveries.db")

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: store.claim("delivery-1", REQUEST), range(8)))

    assert sum(claim.acquired for claim in claims) == 1


def test_store_creates_parent_directory(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / "deliveries.db"

    RemediationStore(database_path)

    assert database_path.exists()


@pytest.mark.parametrize("status", list(RemediationStatus))
def test_store_status_round_trip(tmp_path: Path, status: RemediationStatus) -> None:
    store = RemediationStore(tmp_path / f"{status.value}.db")
    store.claim("delivery-1", REQUEST)
    if status is RemediationStatus.DISPATCHED:
        store.mark_dispatched("delivery-1", "devin-1", "https://app.devin.ai/sessions/1")
    elif status is RemediationStatus.FAILED:
        store.mark_failed("delivery-1", "boom")

    assert stored_status(tmp_path / f"{status.value}.db", "delivery-1") == status.value


def test_retry_increments_attempts_and_clears_error(tmp_path: Path) -> None:
    store = RemediationStore(tmp_path / "deliveries.db")
    store.claim("delivery-1", REQUEST)
    store.mark_failed("delivery-1", "Devin API request failed: 500")

    failed = store.get("delivery-1")
    assert failed is not None
    assert failed.attempts == 1
    assert failed.last_error == "Devin API request failed: 500"
    assert failed.dispatched_at is None

    claim = store.claim("delivery-1", REQUEST)
    store.mark_dispatched("delivery-1", "devin-1", "https://app.devin.ai/sessions/1")

    retried = store.get("delivery-1")
    assert claim.acquired
    assert retried is not None
    assert retried.attempts == 2
    assert retried.last_error is None
    assert retried.dispatched_at is not None


def test_claim_records_issue_metadata(tmp_path: Path) -> None:
    store = RemediationStore(tmp_path / "deliveries.db")

    job = store.claim("delivery-1", REQUEST).job

    assert (job.repository, job.issue_number, job.issue_title, job.issue_url) == (
        REQUEST.repository_full_name,
        REQUEST.issue_number,
        REQUEST.issue_title,
        REQUEST.issue_url,
    )
    assert job.status is RemediationStatus.IN_PROGRESS
