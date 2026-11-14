from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from harness import Harness, MemoryObjectStore
from hmm_core.platform.config import Config
from hmm_core.platform.storage import S3Storage, StorageError, create_storage


def test_storage_config() -> None:
    env = {
        "GITHUB_TOKEN": "test-token",
        "S3_ENDPOINT": "https://s3.test/",
        "S3_ACCESS_KEY": "hmm-access",
        "S3_SECRET_KEY": "hmm-secret",
        "S3_BUCKET": "hmm",
    }
    config = Config.from_env(env)
    assert config.github_token == "test-token"
    assert config.s3_endpoint == "https://s3.test/"
    assert config.s3_access_key == "hmm-access"
    assert config.s3_secret_key == "hmm-secret"
    assert config.s3_bucket == "hmm"
    assert isinstance(create_storage(config), S3Storage)
    for empty_env in ({}, dict.fromkeys(env, "")):
        empty = Config.from_env(empty_env)
        assert empty.github_token is None
        assert empty.s3_endpoint is None
        assert empty.s3_access_key is None
        assert empty.s3_secret_key is None
        assert empty.s3_bucket is None
        assert create_storage(empty) is None
    for key in ("S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET"):
        assert create_storage(Config.from_env({k: v for k, v in env.items() if k != key})) is None


async def test_presign_get() -> None:
    store = S3Storage(
        endpoint="http://s3.test:9000/",
        access_key="hmm-access",
        secret_key="hmm-secret",
        bucket="hmm",
    )
    url = urlsplit(await store.presign_get("cv/u/abc", timedelta(minutes=5)))
    assert url.netloc == "s3.test:9000"
    assert url.path == "/hmm/cv/u/abc"
    query = parse_qs(url.query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == ["300"]
    assert "X-Amz-Credential=hmm-access%2F" in url.query
    assert query["X-Amz-Signature"][0]
    for expires in (timedelta(0), timedelta(days=8)):
        with pytest.raises(ValueError):
            await store.presign_get("cv/u/abc", expires)


async def test_storage_connection_errors() -> None:
    store = S3Storage(
        endpoint="http://127.0.0.1:9",
        access_key="hmm-access",
        secret_key="hmm-secret",
        bucket="hmm",
    )
    with pytest.raises(StorageError):
        await store.put("cv/u/abc", b"data", content_type="text/plain")
    with pytest.raises(StorageError):
        await store.get("cv/u/abc")


async def test_harness_injection(harness: Harness) -> None:
    assert harness.app.state.storage is None
    assert harness.runtime.storage is None
    assert harness.runtime.config.github_token == "test-token"
    store = MemoryObjectStore()
    harness.inject_storage(store)
    assert harness.app.state.storage is store
    assert harness.runtime.storage is store
    await store.put("k", b"v", content_type="text/plain")
    assert store.puts == ["k"]
    assert store.content_types == {"k": "text/plain"}
    assert await store.get("k") == b"v"
    assert store.gets == ["k"]
    await store.delete("k")
    assert store.deleted == ["k"]
    assert store.objects == {}
    assert store.content_types == {}
    assert (
        await store.presign_get("k", timedelta(minutes=5)) == "https://storage.test/k?expires=300"
    )
    with pytest.raises(KeyError):
        await store.get("k")
    await store.delete("missing")
    assert store.deleted == ["k", "missing"]
    async with httpx.AsyncClient() as http:
        harness.inject_http(http)
        assert harness.app.state.http is http
        assert harness.runtime.http is http
        assert harness.runtime.storage is store
