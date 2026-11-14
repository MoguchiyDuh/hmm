from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, Protocol

import aioboto3
from botocore.config import Config as S3Config
from botocore.exceptions import BotoCoreError, ClientError

from hmm_core.platform.config import Config


class StorageError(Exception):
    pass


class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes, *, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    async def presign_get(self, key: str, expires: timedelta) -> str: ...


class S3Storage:
    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "us-east-1",
    ) -> None:
        self._session = aioboto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )
        self._endpoint = endpoint.rstrip("/")
        self._bucket = bucket
        self._config = S3Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 2, "mode": "standard"},
            connect_timeout=10,
            read_timeout=30,
        )

    @asynccontextmanager
    async def _client(self) -> AsyncGenerator[Any]:
        async with self._session.client(
            "s3", endpoint_url=self._endpoint, config=self._config
        ) as client:
            yield client

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        try:
            async with self._client() as client:
                await client.put_object(
                    Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
                )
        except (BotoCoreError, ClientError) as error:
            raise StorageError(str(error)) from error

    async def get(self, key: str) -> bytes:
        try:
            async with self._client() as client:
                response = await client.get_object(Bucket=self._bucket, Key=key)
                async with response["Body"] as stream:
                    return await stream.read()
        except (BotoCoreError, ClientError) as error:
            raise StorageError(str(error)) from error

    async def delete(self, key: str) -> None:
        try:
            async with self._client() as client:
                await client.delete_object(Bucket=self._bucket, Key=key)
        except (BotoCoreError, ClientError) as error:
            raise StorageError(str(error)) from error

    async def presign_get(self, key: str, expires: timedelta) -> str:
        seconds = int(expires.total_seconds())
        if not 1 <= seconds <= 604800:
            raise ValueError("Presigned URL expiry must be between 1 and 604800 seconds")
        async with self._client() as client:
            return await client.generate_presigned_url(
                "get_object", Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=seconds
            )


def create_storage(config: Config) -> S3Storage | None:
    if not (
        config.s3_endpoint and config.s3_access_key and config.s3_secret_key and config.s3_bucket
    ):
        return None
    return S3Storage(
        endpoint=config.s3_endpoint,
        access_key=config.s3_access_key,
        secret_key=config.s3_secret_key,
        bucket=config.s3_bucket,
    )
