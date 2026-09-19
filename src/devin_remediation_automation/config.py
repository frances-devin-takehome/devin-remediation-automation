import os
from dataclasses import dataclass
from functools import lru_cache

DEFAULT_DEVIN_API_BASE_URL = "https://api.devin.ai"
DEFAULT_ALLOWED_REPOSITORY = "frances-devin-takehome/superset"


@dataclass(frozen=True)
class Settings:
    github_webhook_secret: str
    devin_api_key: str
    devin_org_id: str
    devin_api_base_url: str = DEFAULT_DEVIN_API_BASE_URL
    allowed_repository: str = DEFAULT_ALLOWED_REPOSITORY


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        github_webhook_secret=_required("GITHUB_WEBHOOK_SECRET"),
        devin_api_key=_required("DEVIN_API_KEY"),
        devin_org_id=_required("DEVIN_ORG_ID"),
        devin_api_base_url=os.environ.get("DEVIN_API_BASE_URL", DEFAULT_DEVIN_API_BASE_URL),
        allowed_repository=os.environ.get("ALLOWED_REPOSITORY", DEFAULT_ALLOWED_REPOSITORY),
    )
