import json

import httpx
import pytest

from devin_remediation_automation.devin_client import DevinAPIError, DevinClient


def make_client(handler) -> DevinClient:
    transport = httpx.MockTransport(handler)
    return DevinClient(
        httpx.AsyncClient(transport=transport),
        base_url="https://api.devin.ai",
        api_key="cog_test_key",
        org_id="org-test",
    )


async def test_create_session_posts_to_v3_organization_endpoint() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={"session_id": "devin-abc", "url": "https://app.devin.ai/sessions/abc"},
        )

    session = await make_client(handler).create_session("do the thing", title="T", tags=["x"])

    assert seen["url"] == "https://api.devin.ai/v3/organizations/org-test/sessions"
    assert seen["auth"] == "Bearer cog_test_key"
    assert seen["body"] == {"prompt": "do the thing", "title": "T", "tags": ["x"]}
    assert session.session_id == "devin-abc"
    assert session.url == "https://app.devin.ai/sessions/abc"


async def test_error_status_raises_devin_api_error() -> None:
    client = make_client(lambda request: httpx.Response(500, json={"detail": "boom"}))

    with pytest.raises(DevinAPIError):
        await client.create_session("do the thing")


async def test_transport_failure_raises_devin_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(DevinAPIError):
        await make_client(handler).create_session("do the thing")


async def test_malformed_response_raises_devin_api_error() -> None:
    client = make_client(lambda request: httpx.Response(200, json={"session_id": "devin-abc"}))

    with pytest.raises(DevinAPIError):
        await client.create_session("do the thing")
