import os
from collections.abc import Mapping
from typing import Self

from hmm_contracts.common import Contract
from pydantic import AnyHttpUrl, field_validator


class Config(Contract):
    public_url: str

    @field_validator("public_url")
    @classmethod
    def validate_public_url(cls, value: str) -> str:
        AnyHttpUrl(value)
        return value.rstrip("/")

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Self:
        return cls(public_url=env.get("PUBLIC_URL", "http://localhost:8000"))
