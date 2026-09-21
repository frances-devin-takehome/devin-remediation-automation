import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from devin_remediation_automation.api.analytics import DEFAULT_WINDOW_DAYS, SECONDS_PER_DAY
from devin_remediation_automation.devin_client import DevinAPIError, DevinClient
from helpers import FakeDevinClient, build_app, labeled_payload, signed_request


def metrics_client(handler) -> DevinClient:
    return DevinClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        base_url="https://api.devin.ai",
        api_key="cog_test_key",
        org_id="org-test",
    )


async def test_client_requests_the_v3_session_metrics_endpoint() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "sessions_created_count": 12,
                "avg_acus_per_session": 3.5,
                "sessions_with_merged_prs_count": 7,
                "sessions_created_by_origin": {"api": 9, "webapp": 3},
                "sessions_created_by_size": {"s": 4, "m": 8},
                "sessions_created_with_playbook_count": 1,
            },
        )

    metrics = await metrics_client(handler).get_session_metrics(time_after=1000, time_before=2000)

    assert seen["url"] == (
        "https://api.devin.ai/v3/organizations/org-test/metrics/sessions"
        "?time_after=1000&time_before=2000"
    )
    assert seen["auth"] == "Bearer cog_test_key"
    assert metrics.sessions_created_count == 12
    assert metrics.avg_acus_per_session == 3.5
    assert metrics.sessions_with_merged_prs_count == 7
    assert metrics.sessions_created_by_origin == {"api": 9, "webapp": 3}
    assert metrics.sessions_created_by_size == {"s": 4, "m": 8}


async def test_client_raises_on_upstream_error_status() -> None:
    client = metrics_client(lambda request: httpx.Response(403, json={"title": "Forbidden"}))

    with pytest.raises(DevinAPIError):
        await client.get_session_metrics(time_after=1000, time_before=2000)


async def test_client_raises_on_incomplete_metrics_body() -> None:
    client = metrics_client(lambda request: httpx.Response(200, json={"avg_acus_per_session": 1}))

    with pytest.raises(DevinAPIError):
        await client.get_session_metrics(time_after=1000, time_before=2000)


def test_analytics_endpoint_returns_the_evaluator_subset(tmp_path: Path) -> None:
    devin_client = FakeDevinClient()
    app = build_app(devin_client, str(tmp_path / "deliveries.db"))

    with TestClient(app) as client:
        before = int(time.time())
        response = client.get("/devin/analytics")

    assert response.status_code == 200
    body = response.json()
    assert body["sessions_created"] == 12
    assert body["avg_acus_per_session"] == 3.5
    assert body["sessions_with_merged_prs"] == 7
    assert body["sessions_created_by_origin"] == {"api": 9, "webapp": 3}
    assert body["sessions_created_by_size"] == {"s": 4, "m": 8}
    assert body["window"]["days"] == DEFAULT_WINDOW_DAYS

    window = devin_client.metrics_calls[0]
    assert window["time_before"] >= before
    assert window["time_before"] - window["time_after"] == DEFAULT_WINDOW_DAYS * SECONDS_PER_DAY


def test_analytics_endpoint_accepts_an_explicit_window(tmp_path: Path) -> None:
    devin_client = FakeDevinClient()
    app = build_app(devin_client, str(tmp_path / "deliveries.db"))

    with TestClient(app) as client:
        response = client.get("/devin/analytics", params={"time_after": 100, "time_before": 200})

    assert response.json()["window"]["time_after"] == 100
    assert devin_client.metrics_calls == [{"time_after": 100, "time_before": 200}]


def test_analytics_endpoint_rejects_an_inverted_window(tmp_path: Path) -> None:
    devin_client = FakeDevinClient()
    app = build_app(devin_client, str(tmp_path / "deliveries.db"))

    with TestClient(app) as client:
        response = client.get("/devin/analytics", params={"time_after": 200, "time_before": 100})

    assert response.status_code == 400
    assert devin_client.metrics_calls == []


def test_analytics_endpoint_reports_upstream_failure_as_bad_gateway(tmp_path: Path) -> None:
    devin_client = FakeDevinClient(metrics_error=DevinAPIError("Devin API returned 403"))
    app = build_app(devin_client, str(tmp_path / "deliveries.db"))

    with TestClient(app) as client:
        response = client.get("/devin/analytics")

    assert response.status_code == 502


def test_analytics_failure_does_not_affect_the_webhook_path(tmp_path: Path) -> None:
    devin_client = FakeDevinClient(metrics_error=DevinAPIError("Devin API returned 403"))
    app = build_app(devin_client, str(tmp_path / "deliveries.db"))

    with TestClient(app) as client:
        assert client.get("/devin/analytics").status_code == 502
        body, headers = signed_request(labeled_payload(), delivery_id="delivery-1")
        webhook = client.post("/webhooks/github", content=body, headers=headers)

    assert webhook.json()["status"] == "dispatched"
