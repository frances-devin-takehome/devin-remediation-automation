from fastapi import FastAPI

from devin_remediation_automation import __version__
from devin_remediation_automation.api.health import router as health_router


def create_app() -> FastAPI:
    app = FastAPI(title="Devin Remediation Automation", version=__version__)
    app.include_router(health_router)
    return app


app = create_app()
