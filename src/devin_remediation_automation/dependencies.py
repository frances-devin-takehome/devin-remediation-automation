from functools import cache
from threading import Lock
from typing import Annotated

import httpx
from fastapi import Depends, Request

from devin_remediation_automation.config import Settings, get_settings
from devin_remediation_automation.devin_client import DevinClient
from devin_remediation_automation.remediation_store import RemediationStore


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


_store_lock = Lock()


@cache
def _remediation_store(database_path: str) -> RemediationStore:
    return RemediationStore(database_path)


def get_remediation_store(
    settings: Annotated[Settings, Depends(get_settings)],
) -> RemediationStore:
    # Requests are served from a threadpool, so serialize the one-time schema setup.
    with _store_lock:
        return _remediation_store(settings.delivery_db_path)


def get_devin_client(
    settings: Annotated[Settings, Depends(get_settings)],
    http_client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> DevinClient:
    return DevinClient(
        http_client,
        base_url=settings.devin_api_base_url,
        api_key=settings.devin_api_key,
        org_id=settings.devin_org_id,
    )
