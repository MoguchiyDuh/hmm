from uuid import UUID

import httpx
from harness import Harness, MemoryObjectStore
from hmm_contracts.common import ErrorCode
from hmm_core.models import CvFile, User
from hmm_core.platform.settings import SettingKey, set_setting
from hmm_core.profiles.schemas import UploadCvBody
from sqlalchemy import select
from test_api_skeleton import assert_problem

CV_URL = "/api/v1/me/cv"


async def upload(client: httpx.AsyncClient, filename: str, data: bytes) -> httpx.Response:
    return await client.put(CV_URL, files={"file": (filename, data, "text/markdown")})


async def cv_rows(h: Harness) -> list[CvFile]:
    async with h.db() as session:
        return list(await session.scalars(select(CvFile)))


async def seed_cv(h: Harness, user: User, **fields: object) -> CvFile:
    row = CvFile(
        user_id=user.id,
        object_key=f"cv/{user.id}/seeded",
        filename="cv.pdf",
        mime="application/pdf",
        size_bytes=10,
        uploaded_at=h.now,
        text=None,
        text_status="ok",
        text_error=None,
    )
    for name, value in fields.items():
        setattr(row, name, value)
    async with h.db() as session:
        session.add(row)
    return row


async def test_upload_markdown(harness: Harness) -> None:
    store = MemoryObjectStore()
    harness.inject_storage(store)
    user = await harness.user()
    async with await harness.client(user) as client:
        response = await upload(client, "cv.md", b"# hi\n")
    assert response.status_code == 202, response.text
    assert response.json() == {
        "filename": "cv.md",
        "text_status": "pending",
        "allowed_actions": ["replace", "delete"],
    }
    rows = await cv_rows(harness)
    assert len(rows) == 1
    row = rows[0]
    assert row.mime == "text/markdown"
    assert row.size_bytes == 5
    assert row.uploaded_at == harness.now
    assert row.text is None
    assert row.text_status == "pending"
    prefix = f"cv/{user.id}/"
    assert row.object_key.startswith(prefix)
    UUID(row.object_key.removeprefix(prefix))
    assert store.content_types[row.object_key] == "text/markdown"
    assert store.objects[row.object_key] == b"# hi\n"
    jobs = await harness.pending_jobs("cv_extract")
    assert len(jobs) == 1
    assert jobs[0]["lock"] == f"user:{user.id}"
    assert jobs[0]["args"] == {"user_id": str(user.id)}


async def test_upload_typst_and_pdf(harness: Harness) -> None:
    store = MemoryObjectStore()
    harness.inject_storage(store)
    user = await harness.user()
    async with await harness.client(user) as client:
        typst = await upload(client, "cv.typ", b"= Title")
        assert typst.status_code == 202, typst.text
        assert (await cv_rows(harness))[0].mime == "text/plain"
        pdf = await upload(client, "cv.bin", b"%PDF-1.4\n%fake")
        assert pdf.status_code == 202, pdf.text
    row = (await cv_rows(harness))[0]
    assert row.mime == "application/pdf"
    assert store.content_types[row.object_key] == "application/pdf"


async def test_upload_rejects_unsupported(harness: Harness) -> None:
    store = MemoryObjectStore()
    harness.inject_storage(store)
    user = await harness.user()
    async with await harness.client(user) as client:
        for filename, data in (
            ("cv.docx", b"PK\x03\x04"),
            ("cv.md", b"\xff\xfe"),
            ("cv.md", b""),
            ("   ", b"# hi"),
        ):
            response = await upload(client, filename, data)
            assert_problem(response, 415, ErrorCode.UNSUPPORTED_FILE_TYPE)
    assert await cv_rows(harness) == []
    assert await harness.pending_jobs("cv_extract") == []
    assert store.objects == {}
    assert store.puts == []


async def test_upload_size_limit(harness: Harness) -> None:
    store = MemoryObjectStore()
    harness.inject_storage(store)
    admin = await harness.admin()
    user = await harness.user()
    async with await harness.client(user) as client:
        too_large = await upload(client, "cv.md", b"a" * 5_242_881)
        assert_problem(too_large, 413, ErrorCode.FILE_TOO_LARGE)
        async with harness.db() as session:
            await set_setting(session, SettingKey.CV_MAX_BYTES, 1000, actor=admin.id)
        exact = await upload(client, "cv.md", b"a" * 1000)
        assert exact.status_code == 202, exact.text
        over = await upload(client, "cv.md", b"a" * 1001)
        assert_problem(over, 413, ErrorCode.FILE_TOO_LARGE)
    rows = await cv_rows(harness)
    assert len(rows) == 1
    assert rows[0].size_bytes == 1000
    assert list(store.objects) == [rows[0].object_key]
    assert "maxLength" not in UploadCvBody.model_json_schema()["properties"]["file"]


async def test_upload_replaces_previous(harness: Harness) -> None:
    store = MemoryObjectStore()
    harness.inject_storage(store)
    user = await harness.user()
    async with await harness.client(user) as client:
        first = await upload(client, "cv.md", b"alpha")
        assert first.status_code == 202, first.text
        key_a = (await cv_rows(harness))[0].object_key
        second = await upload(client, "other.md", b"beta!")
        assert second.status_code == 202, second.text
    rows = await cv_rows(harness)
    assert len(rows) == 1
    row = rows[0]
    assert row.object_key != key_a
    assert row.filename == "other.md"
    assert row.size_bytes == 5
    assert row.text_status == "pending"
    assert store.deleted == [key_a]
    assert key_a not in store.objects
    assert store.objects[row.object_key] == b"beta!"
    assert len(await harness.pending_jobs("cv_extract")) == 1


async def test_upload_requires_file_field(harness: Harness) -> None:
    harness.inject_storage(MemoryObjectStore())
    user = await harness.user()
    async with await harness.client(user) as client:
        assert_problem(
            await client.put(CV_URL, data={"file": "x"}), 422, ErrorCode.VALIDATION_ERROR
        )
        assert_problem(await client.put(CV_URL), 422, ErrorCode.VALIDATION_ERROR)


async def test_upload_before_onboarding(harness: Harness) -> None:
    harness.inject_storage(MemoryObjectStore())
    user = await harness.user(onboarded=False)
    async with await harness.client(user) as client:
        response = await upload(client, "cv.md", b"# hi\n")
    assert response.status_code == 202, response.text


async def test_delete_cv(harness: Harness) -> None:
    store = MemoryObjectStore()
    harness.inject_storage(store)
    user = await harness.user()
    async with await harness.client(user) as client:
        assert (await upload(client, "cv.md", b"# hi\n")).status_code == 202
        key = (await cv_rows(harness))[0].object_key
        response = await client.delete(CV_URL)
        assert response.status_code == 204, response.text
        assert await cv_rows(harness) == []
        assert key in store.deleted
        jobs = await harness.pending_jobs("recompute_member")
        assert len(jobs) == 1
        assert jobs[0]["args"] == {"user_id": str(user.id)}
        deleted = list(store.deleted)
        again = await client.delete(CV_URL)
        assert again.status_code == 204, again.text
    assert await harness.pending_jobs("recompute_member") == jobs
    assert store.deleted == deleted


async def test_owner_download(harness: Harness) -> None:
    harness.inject_storage(MemoryObjectStore())
    user = await harness.user()
    row = await seed_cv(harness, user)
    async with await harness.client(user) as client:
        response = await client.get(f"/api/v1/users/{user.id}/cv")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "filename": "cv.pdf",
        "url": f"https://storage.test/{row.object_key}?expires=300",
        "text_status": "ok",
        "text_error": None,
        "allowed_actions": ["replace", "delete"],
    }


async def test_download_visibility(harness: Harness) -> None:
    harness.inject_storage(MemoryObjectStore())
    owner = await harness.user()
    other = await harness.user()
    admin = await harness.admin()
    row = await seed_cv(harness, owner, text_status="failed", text_error="bad pdf")
    url = f"/api/v1/users/{owner.id}/cv"
    expected_url = f"https://storage.test/{row.object_key}?expires=300"
    async with await harness.client(owner) as client:
        mine = (await client.get(url)).json()
    assert mine["text_status"] == "failed"
    assert mine["text_error"] == "bad pdf"
    async with await harness.client(other) as client:
        response = await client.get(url)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "filename": "cv.pdf",
        "url": expected_url,
        "text_status": None,
        "text_error": None,
        "allowed_actions": [],
    }
    async with await harness.client(admin) as client:
        response = await client.get(url)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "filename": "cv.pdf",
        "url": expected_url,
        "text_status": "failed",
        "text_error": "bad pdf",
        "allowed_actions": [],
    }


async def test_download_missing(harness: Harness) -> None:
    harness.inject_storage(MemoryObjectStore())
    user = await harness.user()
    other = await harness.user()
    async with await harness.client(user) as client:
        for target in (user, other):
            response = await client.get(f"/api/v1/users/{target.id}/cv")
            assert_problem(response, 404, ErrorCode.NOT_FOUND)


async def test_upload_without_storage(harness: Harness) -> None:
    user = await harness.user()
    async with await harness.client(user) as client:
        response = await upload(client, "cv.md", b"# hi\n")
    assert_problem(response, 500, ErrorCode.INTERNAL_ERROR)
    assert await cv_rows(harness) == []
    assert await harness.pending_jobs("cv_extract") == []
