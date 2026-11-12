import os
from collections.abc import Mapping
from typing import Self

from hmm_contracts.common import Contract
from pydantic import AnyHttpUrl, field_validator


class Config(Contract):
    public_url: str
    database_url: str | None = None
    github_client_id: str | None = None
    github_client_secret: str | None = None
    session_secret: str | None = None
    admin_github_logins: frozenset[str] = frozenset()

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
            admin_github_logins=frozenset(env.get("ADMIN_GITHUB_LOGINS", "").split(",")),
        )
