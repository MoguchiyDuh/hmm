import os
from collections.abc import Mapping
from typing import Self

from hmm_contracts.algo import LLMConfig
from hmm_contracts.common import Contract
from pydantic import AnyHttpUrl, SecretStr, field_validator


class Config(Contract):
    public_url: str
    database_url: str | None = None
    github_client_id: str | None = None
    github_client_secret: str | None = None
    session_secret: str | None = None
    bot_service_token: str | None = None
    parser_service_token: str | None = None
    telegram_bot_username: str | None = None
    admin_github_logins: frozenset[str] = frozenset()
    algorithm: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None

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
            session_secret=env.get("SESSION_SECRET") or None,
            bot_service_token=env.get("BOT_SERVICE_TOKEN") or None,
            parser_service_token=env.get("PARSER_SERVICE_TOKEN") or None,
            telegram_bot_username=env.get("TELEGRAM_BOT_USERNAME") or None,
            admin_github_logins=frozenset(env.get("ADMIN_GITHUB_LOGINS", "").split(",")),
            algorithm=env.get("ALGORITHM") or None,
            llm_base_url=env.get("LLM_BASE_URL") or None,
            llm_api_key=env.get("LLM_API_KEY") or None,
            llm_model=env.get("LLM_MODEL") or None,
        )
