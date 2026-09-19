import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    github_webhook_secret: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if not secret:
        raise RuntimeError("GITHUB_WEBHOOK_SECRET is not set")
    return Settings(github_webhook_secret=secret)
