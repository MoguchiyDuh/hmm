import httpx
import pytest
from harness import Harness, MemoryObjectStore
from hmm_contracts.common import CvTextStatus, GithubSnapshotStatus
from hmm_core.models import CvFile, GithubSnapshot, User
from hmm_core.platform.jobs import REGISTRY, JobName
from hmm_core.profiles import jobs as profile_jobs
from hmm_core.profiles import service as profiles
from hmm_core.profiles.service import RegistrationInputs, inputs_settled, registration_inputs
from sqlalchemy import update

CV_URL = "/api/v1/me/cv"


def sample_pdf() -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length 40 >>\nstream\nBT /F1 24 Tf 72 720 Td (Hello CV) Tj ET\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    document = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(document))
        document += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(document)
    document += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        document += b"%010d 00000 n \n" % offset
    document += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    document += b"startxref\n%d\n%%%%EOF\n" % xref
    return bytes(document)


async def setup(harness: Harness) -> tuple[MemoryObjectStore, User]:
    store = MemoryObjectStore()
    harness.inject_storage(store)
    return store, await harness.user()


async def upload(harness: Harness, user: User, filename: str, data: bytes) -> CvFile:
    async with await harness.client(user) as client:
        response: httpx.Response = await client.put(
            CV_URL, files={"file": (filename, data, "application/octet-stream")}
        )
    assert response.status_code == 202, response.text
    return await cv_row(harness, user)


async def cv_row(harness: Harness, user: User) -> CvFile:
    async with harness.db() as session:
        row = await session.get(CvFile, user.id)
    assert row is not None
    return row


async def extract(harness: Harness, user: User) -> CvFile:
    await profile_jobs.cv_extract(harness.runtime, str(user.id))
    return await cv_row(harness, user)


async def recompute_jobs(harness: Harness) -> list[dict[str, object]]:
    return await harness.pending_jobs("recompute_member")


async def test_extract_markdown(harness: Harness) -> None:
    _, user = await setup(harness)
    await upload(harness, user, "cv.md", b"# hello")
    row = await extract(harness, user)
    assert row.text == "# hello"
    assert row.text_status == "ok"
    assert row.text_error is None
    jobs = await recompute_jobs(harness)
    assert len(jobs) == 1
    assert jobs[0]["args"] == {"user_id": str(user.id)}


async def test_extract_typst(harness: Harness) -> None:
    _, user = await setup(harness)
    uploaded = await upload(harness, user, "cv.typ", b"= Title\nBody")
    assert uploaded.mime == "text/plain"
    row = await extract(harness, user)
    assert row.text == "= Title\nBody"
    assert row.text_status == "ok"
    assert row.text_error is None


async def test_extract_pdf(harness: Harness) -> None:
    _, user = await setup(harness)
    await upload(harness, user, "cv.pdf", sample_pdf())
    row = await extract(harness, user)
    assert row.text is not None
    assert row.text.strip() == "Hello CV"
    assert row.text_status == "ok"


async def test_extract_failure(harness: Harness) -> None:
    _, user = await setup(harness)
    await upload(harness, user, "cv.pdf", b"%PDF-1.4\nnot a pdf")
    row = await extract(harness, user)
    assert row.text is None
    assert row.text_status == "failed"
    assert row.text_error
    assert len(await recompute_jobs(harness)) == 1


async def test_extract_caps_text(harness: Harness) -> None:
    _, user = await setup(harness)
    await upload(harness, user, "cv.md", b"a" * 150_000)
    row = await extract(harness, user)
    assert row.text is not None
    assert len(row.text) == 100_000
    assert row.text_status == "ok"


async def test_extract_idempotent(harness: Harness) -> None:
    store, user = await setup(harness)
    await upload(harness, user, "cv.md", b"# hello")
    first = await extract(harness, user)
    gets = list(store.gets)
    jobs = await recompute_jobs(harness)
    second = await extract(harness, user)
    assert store.gets == gets
    assert await recompute_jobs(harness) == jobs
    assert (second.text, second.text_status, second.text_error, second.updated_at) == (
        first.text,
        first.text_status,
        first.text_error,
        first.updated_at,
    )


async def test_extract_ignores_stale_work(harness: Harness) -> None:
    store, user = await setup(harness)
    key_a = (await upload(harness, user, "cv.md", b"A text")).object_key
    key_b = (await upload(harness, user, "other.md", b"B text")).object_key
    assert store.objects[key_b] == b"B text"
    row = await extract(harness, user)
    assert row.object_key == key_b
    assert row.text == "B text"
    async with harness.db() as session:
        assert (
            await profiles.write_cv_text(
                session,
                harness.jobs,
                user_id=user.id,
                object_key=key_a,
                text="A text",
                status=CvTextStatus.OK,
                error=None,
                now=harness.now,
            )
            is False
        )
    assert (await cv_row(harness, user)).text == "B text"
    async with harness.db() as session:
        stored = await session.get(CvFile, user.id)
        assert stored is not None
        await session.delete(stored)
    gets = list(store.gets)
    await profiles.extract_cv(harness.runtime, user.id)
    assert store.gets == gets


async def test_extract_storage_outage(harness: Harness) -> None:
    store, user = await setup(harness)
    uploaded = await upload(harness, user, "cv.md", b"# hello")
    store.objects.pop(uploaded.object_key)
    with pytest.raises(KeyError):
        await profile_jobs.cv_extract(harness.runtime, str(user.id))
    row = await cv_row(harness, user)
    assert row.text_status == "pending"
    assert row.text is None
    assert await recompute_jobs(harness) == []


async def test_registration_inputs(harness: Harness) -> None:
    user = await harness.user()

    async def inputs() -> RegistrationInputs:
        async with harness.db() as session:
            return await registration_inputs(session, user.id)

    fresh = await inputs()
    assert (fresh.github, fresh.cv) == (None, None)
    assert inputs_settled(fresh) is False
    async with harness.db() as session:
        session.add(
            GithubSnapshot(
                user_id=user.id,
                fetched_at=harness.now,
                status=GithubSnapshotStatus.ERROR,
                error="boom",
            )
        )
    github_only = await inputs()
    assert github_only.github == GithubSnapshotStatus.ERROR
    assert github_only.cv is None
    assert inputs_settled(github_only) is True
    async with harness.db() as session:
        session.add(
            CvFile(
                user_id=user.id,
                object_key=f"cv/{user.id}/seeded",
                filename="cv.md",
                mime="text/markdown",
                size_bytes=1,
                uploaded_at=harness.now,
                text=None,
                text_status=CvTextStatus.PENDING,
                text_error=None,
            )
        )
    assert inputs_settled(await inputs()) is False
    for status in (CvTextStatus.FAILED, CvTextStatus.OK):
        async with harness.db() as session:
            await session.execute(
                update(CvFile).where(CvFile.user_id == user.id).values(text_status=status)
            )
        current = await inputs()
        assert current.cv == status
        assert inputs_settled(current) is True


async def test_cv_extract_registered(harness: Harness) -> None:
    assert JobName.CV_EXTRACT in REGISTRY
