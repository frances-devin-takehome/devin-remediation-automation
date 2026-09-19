import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from devin_remediation_automation.config import Settings, get_settings
from devin_remediation_automation.dependencies import get_devin_client
from devin_remediation_automation.devin_client import DevinAPIError, DevinClient
from devin_remediation_automation.remediation import (
    REMEDIATION_LABEL,
    RemediationRequest,
    build_session_prompt,
    build_session_title,
)
from devin_remediation_automation.security import verify_signature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class WebhookResponse(BaseModel):
    status: str
    devin_session_id: str | None = None
    devin_session_url: str | None = None


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
    x_github_event: Annotated[str | None, Header()] = None,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
) -> WebhookResponse:
    body = await request.body()
    if not verify_signature(settings.github_webhook_secret, body, x_hub_signature_256):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook signature")

    if x_github_event != "issues":
        return WebhookResponse(status="ignored")

    try:
        payload: Any = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed JSON payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed JSON payload")

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

    logger.info(
        "Remediation-eligible issue labeled: repository=%s issue_number=%s title=%s url=%s",
        remediation.repository_full_name,
        remediation.issue_number,
        remediation.issue_title,
        remediation.issue_url,
    )

    try:
        session = await devin_client.create_session(
            build_session_prompt(remediation),
            title=build_session_title(remediation),
            tags=["remediation", "github-issue"],
        )
    except DevinAPIError:
        logger.exception(
            "Failed to create Devin session: repository=%s issue_number=%s",
            remediation.repository_full_name,
            remediation.issue_number,
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Failed to create Devin session") from None

    logger.info(
        "Created Devin session: repository=%s issue_number=%s devin_session_id=%s devin_url=%s",
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
