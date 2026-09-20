import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from devin_remediation_automation import __version__
from devin_remediation_automation.api.health import router as health_router
from devin_remediation_automation.api.remediations import router as remediations_router
from devin_remediation_automation.api.webhooks import router as webhooks_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with httpx.AsyncClient() as http_client:
        app.state.http_client = http_client
        yield


def create_app() -> FastAPI:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    app = FastAPI(title="Devin Remediation Automation", version=__version__, lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(remediations_router)
    app.include_router(webhooks_router)
    return app


app = create_app()
