from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://seats:seats@localhost:5432/seats"
    db_pool_max: int = 20
    # How long a request may wait for a pooled connection before we give up.
    db_acquire_timeout_s: float = 30.0
    # Readiness probe must answer fast; a slow DB counts as not ready.
    readiness_timeout_s: float = 2.0

    # Bearer token for admin endpoints (POST /shows). Override in every real deploy.
    admin_token: str = "dev-admin-token"

    # HS256 secret for user tokens. Override in every real deploy.
    jwt_secret: str = "dev-jwt-secret-change-me-0123456789"
    token_ttl_s: int = 24 * 3600
    allow_token_issue: bool = True

    default_per_user_limit: int = 4
    default_hold_ttl_seconds: int = 300
    max_seats_per_show: int = 20000
    max_seats_per_request: int = 10
    # Retries for transient DB conflicts (deadlock / serialization) before giving up.
    reserve_max_attempts: int = 3

    log_level: str = "INFO"
    port: int = 8000


@lru_cache
def get_settings() -> Settings:
    return Settings()
