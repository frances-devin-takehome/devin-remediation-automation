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


@dataclass(frozen=True)
class DevinSessionMetrics:
    """Subset of the v3 session metrics response used for the analytics view."""

    sessions_created_count: int
    avg_acus_per_session: float
    sessions_with_merged_prs_count: int
    sessions_created_by_origin: dict[str, int]
    sessions_created_by_size: dict[str, int]


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

    async def get_session_metrics(
        self, *, time_after: int, time_before: int
    ) -> DevinSessionMetrics:
        """Fetch organization session metrics for a window of Unix timestamps in seconds.

        Requires a service user with the `ViewOrgMetrics` organization permission.
        """
        url = f"{self._base_url}/v3/organizations/{self._org_id}/metrics/sessions"

        try:
            response = await self._http_client.get(
                url,
                params={"time_after": time_after, "time_before": time_before},
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise DevinAPIError(
                f"Devin API returned {exc.response.status_code} for session metrics"
            ) from exc
        except httpx.HTTPError as exc:
            raise DevinAPIError("Could not reach the Devin API") from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise DevinAPIError("Devin API returned a non-JSON response") from exc
        if not isinstance(body, dict):
            raise DevinAPIError("Devin API returned an unexpected session metrics response")

        sessions_created = body.get("sessions_created_count")
        avg_acus = body.get("avg_acus_per_session")
        merged_prs = body.get("sessions_with_merged_prs_count")
        if (
            not isinstance(sessions_created, int)
            or not isinstance(avg_acus, int | float)
            or not isinstance(merged_prs, int)
        ):
            raise DevinAPIError("Devin API session metrics response is missing required counts")

        return DevinSessionMetrics(
            sessions_created_count=sessions_created,
            avg_acus_per_session=float(avg_acus),
            sessions_with_merged_prs_count=merged_prs,
            sessions_created_by_origin=_counts(body.get("sessions_created_by_origin")),
            sessions_created_by_size=_counts(body.get("sessions_created_by_size")),
        )


def _counts(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {key: count for key, count in value.items() if isinstance(count, int)}
