from functools import cache
from typing import Annotated

import httpx
from fastapi import Depends, Request

from devin_remediation_automation.config import Settings, get_settings
from devin_remediation_automation.delivery_store import DeliveryStore
from devin_remediation_automation.devin_client import DevinClient


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


@cache
def _delivery_store(database_path: str) -> DeliveryStore:
    return DeliveryStore(database_path)


def get_delivery_store(settings: Annotated[Settings, Depends(get_settings)]) -> DeliveryStore:
    return _delivery_store(settings.delivery_db_path)


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
