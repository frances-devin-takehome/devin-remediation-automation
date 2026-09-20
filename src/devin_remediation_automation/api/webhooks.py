import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from devin_remediation_automation.config import Settings, get_settings
from devin_remediation_automation.dependencies import get_devin_client, get_remediation_store
from devin_remediation_automation.devin_client import DevinAPIError, DevinClient
from devin_remediation_automation.remediation import (
    REMEDIATION_LABEL,
    SUCCESS_CONCLUSION,
    VALIDATION_WORKFLOW_NAME,
    RemediationRequest,
    build_session_prompt,
    build_session_title,
    extract_remediation_id,
)
from devin_remediation_automation.remediation_store import RemediationStatus, RemediationStore
from devin_remediation_automation.security import verify_signature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class WebhookResponse(BaseModel):
    status: str
    devin_session_id: str | None = None
    devin_session_url: str | None = None
    remediation_id: str | None = None
    pr_number: int | None = None


def _remediation_request(payload: dict[str, Any]) -> RemediationRequest | None:
    repository = payload.get("repository")
    issue = payload.get("issue")
    if not isinstance(repository, dict) or not isinstance(issue, dict):
        return None

    full_name = repository.get("full_name")
    number = issue.get("number")
    title = issue.get("title")
    url = issue.get("html_url")
    if not isinstance(full_name, str) or not isinstance(number, int) or not isinstance(url, str):
        return None

    body = issue.get("body")
    return RemediationRequest(
        repository_full_name=full_name,
        issue_number=number,
        issue_title=title if isinstance(title, str) else "",
        issue_url=url,
        issue_body=body if isinstance(body, str) else "",
    )


@router.post("/github", response_model=WebhookResponse)
async def receive_github_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    devin_client: Annotated[DevinClient, Depends(get_devin_client)],
    remediation_store: Annotated[RemediationStore, Depends(get_remediation_store)],
    x_github_event: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
) -> WebhookResponse:
    body = await request.body()
    if not verify_signature(settings.github_webhook_secret, body, x_hub_signature_256):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook signature")

    if x_github_event not in ("issues", "pull_request", "workflow_run"):
        return WebhookResponse(status="ignored")

    try:
        payload: Any = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed JSON payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed JSON payload")

    if x_github_event == "pull_request":
        return await _handle_pull_request(payload, settings, remediation_store)

    if x_github_event == "workflow_run":
        return await _handle_workflow_run(payload, settings, remediation_store)

    return await _handle_issue(
        payload, x_github_delivery, settings, devin_client, remediation_store
    )


async def _handle_pull_request(
    payload: dict[str, Any],
    settings: Settings,
    remediation_store: RemediationStore,
) -> WebhookResponse:
    if payload.get("action") != "opened":
        return WebhookResponse(status="ignored")

    repository = payload.get("repository")
    pull_request = payload.get("pull_request")
    if not isinstance(repository, dict) or not isinstance(pull_request, dict):
        return WebhookResponse(status="ignored")

    if repository.get("full_name") != settings.allowed_repository:
        return WebhookResponse(status="ignored")

    remediation_id = extract_remediation_id(pull_request.get("body"))
    number = pull_request.get("number")
    url = pull_request.get("html_url")
    if remediation_id is None or not isinstance(number, int) or not isinstance(url, str):
        return WebhookResponse(status="ignored")

    created_at = pull_request.get("created_at")
    job = await run_in_threadpool(
        remediation_store.record_pull_request,
        remediation_id,
        number,
        url,
        created_at if isinstance(created_at, str) else None,
    )
    if job is None:
        logger.info(
            "Ignoring pull request with unknown remediation marker: remediation_id=%s pr=%s",
            remediation_id,
            url,
        )
        return WebhookResponse(status="ignored")

    # The job keeps its first pull request; a later, different one is reported as a duplicate.
    superseded = job.pr_number != number
    logger.info(
        "Correlated pull request with remediation: remediation_id=%s pr_number=%s pr_url=%s "
        "status=%s",
        remediation_id,
        job.pr_number,
        job.pr_url,
        job.status.value,
    )
    return WebhookResponse(
        status="duplicate" if superseded else "pr_correlated",
        remediation_id=job.delivery_id,
        pr_number=job.pr_number,
        devin_session_id=job.devin_session_id,
        devin_session_url=job.devin_session_url,
    )


async def _handle_workflow_run(
    payload: dict[str, Any],
    settings: Settings,
    remediation_store: RemediationStore,
) -> WebhookResponse:
    repository = payload.get("repository")
    workflow_run = payload.get("workflow_run")
    if not isinstance(repository, dict) or not isinstance(workflow_run, dict):
        return WebhookResponse(status="ignored")

    if (
        repository.get("full_name") != settings.allowed_repository
        or workflow_run.get("name") != VALIDATION_WORKFLOW_NAME
    ):
        return WebhookResponse(status="ignored")

    pr_number = _workflow_pull_request_number(workflow_run)
    run_id = workflow_run.get("id")
    run_url = workflow_run.get("html_url")
    if pr_number is None or not isinstance(run_id, int) or not isinstance(run_url, str):
        return WebhookResponse(status="ignored")

    completed = workflow_run.get("status") == "completed"
    conclusion = workflow_run.get("conclusion")
    conclusion = conclusion if isinstance(conclusion, str) else None
    if not completed:
        job_status = RemediationStatus.CI_RUNNING
        conclusion = None
        completed_at = None
    else:
        job_status = (
            RemediationStatus.SUCCEEDED
            if conclusion == SUCCESS_CONCLUSION
            else RemediationStatus.FAILED
        )
        updated_at = workflow_run.get("updated_at")
        completed_at = updated_at if isinstance(updated_at, str) else None

    job = await run_in_threadpool(
        remediation_store.record_workflow_run,
        pr_number,
        run_id=run_id,
        run_url=run_url,
        status=job_status,
        conclusion=conclusion,
        completed_at=completed_at,
    )
    if job is None:
        logger.info(
            "Ignoring validation workflow run for an unknown pull request: pr_number=%s run=%s",
            pr_number,
            run_url,
        )
        return WebhookResponse(status="ignored")

    logger.info(
        "Recorded validation workflow run: remediation_id=%s pr_number=%s run_id=%s "
        "conclusion=%s status=%s",
        job.delivery_id,
        pr_number,
        run_id,
        job.ci_conclusion,
        job.status.value,
    )
    return WebhookResponse(
        status=job.status.value,
        remediation_id=job.delivery_id,
        pr_number=job.pr_number,
        devin_session_id=job.devin_session_id,
        devin_session_url=job.devin_session_url,
    )


def _workflow_pull_request_number(workflow_run: dict[str, Any]) -> int | None:
    pull_requests = workflow_run.get("pull_requests")
    if not isinstance(pull_requests, list):
        return None
    for pull_request in pull_requests:
        number = pull_request.get("number") if isinstance(pull_request, dict) else None
        if isinstance(number, int):
            return number
    return None


async def _handle_issue(
    payload: dict[str, Any],
    x_github_delivery: str | None,
    settings: Settings,
    devin_client: DevinClient,
    remediation_store: RemediationStore,
) -> WebhookResponse:
    if payload.get("action") != "labeled":
        return WebhookResponse(status="ignored")

    label = payload.get("label")
    label_name = label.get("name") if isinstance(label, dict) else None
    if label_name != REMEDIATION_LABEL:
        return WebhookResponse(status="ignored")

    remediation = _remediation_request(payload)
    if remediation is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing repository or issue in payload")

    if remediation.repository_full_name != settings.allowed_repository:
        logger.info(
            "Ignoring remediation label from repository outside the allowlist: repository=%s "
            "issue_number=%s",
            remediation.repository_full_name,
            remediation.issue_number,
        )
        return WebhookResponse(status="ignored")

    if not x_github_delivery:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing X-GitHub-Delivery header")

    logger.info(
        "Remediation-eligible issue labeled: delivery_id=%s repository=%s issue_number=%s "
        "title=%s url=%s",
        x_github_delivery,
        remediation.repository_full_name,
        remediation.issue_number,
        remediation.issue_title,
        remediation.issue_url,
    )

    claim = await run_in_threadpool(remediation_store.claim, x_github_delivery, remediation)
    if not claim.acquired:
        logger.info(
            "Skipping duplicate delivery: delivery_id=%s status=%s devin_session_id=%s",
            x_github_delivery,
            claim.job.status.value,
            claim.job.devin_session_id,
        )
        duplicate_status = (
            "duplicate" if claim.job.status is RemediationStatus.DISPATCHED else "in_progress"
        )
        return WebhookResponse(
            status=duplicate_status,
            devin_session_id=claim.job.devin_session_id,
            devin_session_url=claim.job.devin_session_url,
        )

    try:
        session = await devin_client.create_session(
            build_session_prompt(remediation, x_github_delivery),
            title=build_session_title(remediation),
            tags=["remediation", "github-issue"],
        )
    except DevinAPIError as exc:
        await run_in_threadpool(remediation_store.mark_failed, x_github_delivery, str(exc))
        logger.exception(
            "Failed to create Devin session: repository=%s issue_number=%s",
            remediation.repository_full_name,
            remediation.issue_number,
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Failed to create Devin session") from None

    await run_in_threadpool(
        remediation_store.mark_dispatched, x_github_delivery, session.session_id, session.url
    )
    logger.info(
        "Created Devin session: delivery_id=%s repository=%s issue_number=%s "
        "devin_session_id=%s devin_url=%s",
        x_github_delivery,
        remediation.repository_full_name,
        remediation.issue_number,
        session.session_id,
        session.url,
    )
    return WebhookResponse(
        status="dispatched",
        devin_session_id=session.session_id,
        devin_session_url=session.url,
    )
