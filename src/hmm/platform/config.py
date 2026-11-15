import os
from collections.abc import Mapping
from typing import Self

from pydantic import AnyHttpUrl, SecretStr, field_validator

from hmm.algo.models import LLMConfig
from hmm.common import Contract


class Config(Contract):
    public_url: str
    database_url: str | None = None
    github_client_id: str | None = None
    github_client_secret: str | None = None
    github_token: str | None = None
    s3_endpoint: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_bucket: str | None = None
    session_secret: str | None = None
    telegram_bot_username: str | None = None
    bot_token: SecretStr | None = None
    admin_github_logins: frozenset[str] = frozenset()
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    parser_sources: str | None = None
    log_level: str = "INFO"

    def llm(self) -> LLMConfig | None:
        if not (self.llm_base_url and self.llm_api_key and self.llm_model):
            return None
        return LLMConfig(
            base_url=self.llm_base_url, api_key=SecretStr(self.llm_api_key), model=self.llm_model
        )

    @field_validator("public_url")
    @classmethod
    def validate_public_url(cls, value: str) -> str:
        AnyHttpUrl(value)
        return value.rstrip("/")

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Unknown log level {value!r}")
        return level

    @field_validator("admin_github_logins")
    @classmethod
    def normalize_admin_logins(cls, value: frozenset[str]) -> frozenset[str]:
        return frozenset(login.strip().lower() for login in value if login.strip())

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Self:
        return cls(
            public_url=env.get("PUBLIC_URL", "http://localhost:8000"),
            database_url=env.get("DATABASE_URL") or None,
            github_client_id=env.get("GITHUB_CLIENT_ID") or None,
            github_client_secret=env.get("GITHUB_CLIENT_SECRET") or None,
            github_token=env.get("GITHUB_TOKEN") or None,
            s3_endpoint=env.get("S3_ENDPOINT") or None,
            s3_access_key=env.get("S3_ACCESS_KEY") or None,
            s3_secret_key=env.get("S3_SECRET_KEY") or None,
            s3_bucket=env.get("S3_BUCKET") or None,
            session_secret=env.get("SESSION_SECRET") or None,
            telegram_bot_username=env.get("TELEGRAM_BOT_USERNAME") or None,
            bot_token=SecretStr(env["BOT_TOKEN"]) if env.get("BOT_TOKEN") else None,
            admin_github_logins=frozenset(env.get("ADMIN_GITHUB_LOGINS", "").split(",")),
            llm_base_url=env.get("LLM_BASE_URL") or None,
            llm_api_key=env.get("LLM_API_KEY") or None,
            llm_model=env.get("LLM_MODEL") or None,
            parser_sources=env.get("PARSER_SOURCES") or None,
            log_level=env.get("LOG_LEVEL") or "INFO",
        )
