import logging
import os

from fastapi import FastAPI

from devin_remediation_automation import __version__
from devin_remediation_automation.api.health import router as health_router
from devin_remediation_automation.api.webhooks import router as webhooks_router


def create_app() -> FastAPI:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    app = FastAPI(title="Devin Remediation Automation", version=__version__)
    app.include_router(health_router)
    app.include_router(webhooks_router)
    return app


app = create_app()
