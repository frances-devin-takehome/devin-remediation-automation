from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from devin_remediation_automation.dependencies import get_remediation_store
from devin_remediation_automation.remediation_store import (
    RemediationJob,
    RemediationStatus,
    RemediationStore,
)

router = APIRouter(tags=["remediations"])


class RemediationJobResponse(BaseModel):
    delivery_id: str
    repository: str
    issue_number: int
    issue_title: str
    issue_url: str
    status: RemediationStatus
    attempts: int
    created_at: str
    updated_at: str
    dispatched_at: str | None = None
    devin_session_id: str | None = None
    devin_session_url: str | None = None
    last_error: str | None = None

    @classmethod
    def of(cls, job: RemediationJob) -> "RemediationJobResponse":
        return cls(**vars(job))


class RemediationListResponse(BaseModel):
    count: int
    jobs: list[RemediationJobResponse]


class RemediationMetricsResponse(BaseModel):
    """Aggregate view of remediation work.

    `dispatched` counts jobs whose Devin session was created; it is not a count of successful
    remediations, which the service cannot determine yet.
    """

    total: int
    counts_by_status: dict[str, int]
    active: int
    dispatched: int
    failed: int
    dispatch_attempts: int
    last_dispatched_at: str | None = None
    oldest_in_progress_at: str | None = None


@router.get("/remediations", response_model=RemediationListResponse)
async def list_remediations(
    store: Annotated[RemediationStore, Depends(get_remediation_store)],
    job_status: Annotated[RemediationStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RemediationListResponse:
    jobs = await run_in_threadpool(store.list_jobs, status=job_status, limit=limit, offset=offset)
    return RemediationListResponse(
        count=len(jobs), jobs=[RemediationJobResponse.of(job) for job in jobs]
    )


@router.get("/remediations/metrics", response_model=RemediationMetricsResponse)
async def get_remediation_metrics(
    store: Annotated[RemediationStore, Depends(get_remediation_store)],
) -> RemediationMetricsResponse:
    summary = await run_in_threadpool(store.summary)
    counts = summary.counts_by_status
    return RemediationMetricsResponse(
        total=summary.total,
        counts_by_status=counts,
        active=counts[RemediationStatus.IN_PROGRESS.value],
        dispatched=counts[RemediationStatus.DISPATCHED.value],
        failed=counts[RemediationStatus.FAILED.value],
        dispatch_attempts=summary.dispatch_attempts,
        last_dispatched_at=summary.last_dispatched_at,
        oldest_in_progress_at=summary.oldest_in_progress_at,
    )


@router.get("/remediations/{delivery_id}", response_model=RemediationJobResponse)
async def get_remediation(
    delivery_id: str,
    store: Annotated[RemediationStore, Depends(get_remediation_store)],
) -> RemediationJobResponse:
    job = await run_in_threadpool(store.get, delivery_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown remediation delivery id")
    return RemediationJobResponse.of(job)
