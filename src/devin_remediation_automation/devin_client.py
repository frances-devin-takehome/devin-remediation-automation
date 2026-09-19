import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


class DevinAPIError(RuntimeError):
    """Raised when the Devin API cannot be reached or returns an unusable response."""


@dataclass(frozen=True)
class DevinSession:
    session_id: str
    url: str


class DevinClient:
    """Minimal client for the Devin v3 Organization API."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: str,
        org_id: str,
        timeout: float = 30.0,
    ) -> None:
        self._http_client = http_client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._org_id = org_id
        self._timeout = timeout

    async def create_session(
        self,
        prompt: str,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
    ) -> DevinSession:
        url = f"{self._base_url}/v3/organizations/{self._org_id}/sessions"
        payload: dict[str, object] = {"prompt": prompt}
        if title:
            payload["title"] = title
        if tags:
            payload["tags"] = tags

        try:
            response = await self._http_client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise DevinAPIError(
                f"Devin API returned {exc.response.status_code} for session creation"
            ) from exc
        except httpx.HTTPError as exc:
            raise DevinAPIError("Could not reach the Devin API") from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise DevinAPIError("Devin API returned a non-JSON response") from exc

        session_id = body.get("session_id") if isinstance(body, dict) else None
        session_url = body.get("url") if isinstance(body, dict) else None
        if not isinstance(session_id, str) or not isinstance(session_url, str):
            raise DevinAPIError("Devin API response is missing session_id or url")

        return DevinSession(session_id=session_id, url=session_url)
