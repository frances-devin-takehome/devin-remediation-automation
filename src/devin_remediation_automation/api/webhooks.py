import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from devin_remediation_automation.config import Settings, get_settings
from devin_remediation_automation.security import verify_signature

logger = logging.getLogger(__name__)

REMEDIATION_LABEL = "devin-remediation"

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class WebhookResponse(BaseModel):
    status: str


@router.post("/github", response_model=WebhookResponse)
async def receive_github_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
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

    repository = payload.get("repository")
    issue = payload.get("issue")
    if not isinstance(repository, dict) or not isinstance(issue, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing repository or issue in payload")

    logger.info(
        "Remediation-eligible issue labeled: repository=%s issue_number=%s title=%s url=%s",
        repository.get("full_name"),
        issue.get("number"),
        issue.get("title"),
        issue.get("html_url"),
    )
    return WebhookResponse(status="accepted")
