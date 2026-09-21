import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from devin_remediation_automation.dependencies import get_devin_client
from devin_remediation_automation.devin_client import DevinAPIError, DevinClient

logger = logging.getLogger(__name__)
router = APIRouter(tags=["analytics"])

DEFAULT_WINDOW_DAYS = 7
SECONDS_PER_DAY = 86400


class AnalyticsWindow(BaseModel):
    """Requested window, as Unix timestamps in seconds (UTC), as the v3 metrics API expects."""

    time_after: int
    time_before: int
    days: float


class DevinAnalyticsResponse(BaseModel):
    """Devin platform utilization, kept separate from application remediation metrics."""

    window: AnalyticsWindow
    sessions_created: int
    avg_acus_per_session: float
    sessions_with_merged_prs: int
    sessions_created_by_origin: dict[str, int]
    sessions_created_by_size: dict[str, int]


@router.get("/devin/analytics", response_model=DevinAnalyticsResponse)
async def get_devin_analytics(
    devin_client: Annotated[DevinClient, Depends(get_devin_client)],
    time_after: Annotated[int | None, Query(ge=0)] = None,
    time_before: Annotated[int | None, Query(ge=0)] = None,
) -> DevinAnalyticsResponse:
    window_end = time_before if time_before is not None else int(time.time())
    window_start = (
        time_after if time_after is not None else window_end - DEFAULT_WINDOW_DAYS * SECONDS_PER_DAY
    )
    if window_start >= window_end:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "time_after must be earlier than time_before"
        )

    try:
        metrics = await devin_client.get_session_metrics(
            time_after=window_start, time_before=window_end
        )
    except DevinAPIError as exc:
        logger.warning("Devin analytics request failed: %s", exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    return DevinAnalyticsResponse(
        window=AnalyticsWindow(
            time_after=window_start,
            time_before=window_end,
            days=(window_end - window_start) / SECONDS_PER_DAY,
        ),
        sessions_created=metrics.sessions_created_count,
        avg_acus_per_session=metrics.avg_acus_per_session,
        sessions_with_merged_prs=metrics.sessions_with_merged_prs_count,
        sessions_created_by_origin=metrics.sessions_created_by_origin,
        sessions_created_by_size=metrics.sessions_created_by_size,
    )
