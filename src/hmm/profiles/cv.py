import io
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from uuid import UUID, uuid4

import procrastinate
import pypdf
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.common import CvTextStatus, ErrorCode
from hmm.identity.service import lock_user
from hmm.platform.db import get_or_404, locked
from hmm.platform.errors import ProblemError
from hmm.platform.jobs import JobName, Runtime, defer_job
from hmm.platform.settings import load_settings
from hmm.platform.storage import ObjectStore

CHUNK_SIZE = 64 * 1024


CV_URL_TTL = timedelta(minutes=5)


def require_storage(store: ObjectStore | None) -> ObjectStore:
    if store is None:
        raise ProblemError(ErrorCode.INTERNAL_ERROR, "Object storage is not configured")
    return store


def _detect_mime(name: str, data: bytes) -> str:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    lowered = name.lower()
    if lowered.endswith((".md", ".typ")):
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise ProblemError(ErrorCode.UNSUPPORTED_FILE_TYPE, "File is not valid UTF-8") from None
        return "text/markdown" if lowered.endswith(".md") else "text/plain"
    raise ProblemError(ErrorCode.UNSUPPORTED_FILE_TYPE)


async def upload_cv(
    session: AsyncSession,
    jobs: procrastinate.App,
    store: ObjectStore,
    *,
    user_id: UUID,
    filename: str | None,
    chunks: AsyncIterator[bytes],
    now: datetime,
) -> models.CvFile:
    max_bytes = (await load_settings(session)).cv_max_bytes
    buffer = bytearray()
    async for chunk in chunks:
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise ProblemError(ErrorCode.FILE_TOO_LARGE, f"CV exceeds {max_bytes} bytes")
    if not buffer:
        raise ProblemError(ErrorCode.UNSUPPORTED_FILE_TYPE, "Empty file")
    name = PurePosixPath((filename or "").replace("\\", "/")).name.strip()[:200]
    if not name:
        raise ProblemError(ErrorCode.UNSUPPORTED_FILE_TYPE, "Missing filename")
    data = bytes(buffer)
    mime = _detect_mime(name, data)
    key = f"cv/{user_id}/{uuid4()}"
    await store.put(key, data, content_type=mime)
    await lock_user(session, user_id)
    row = await locked(session, models.CvFile, user_id)
    old_key: str | None = None
    if row is None:
        row = models.CvFile(
            user_id=user_id,
            object_key=key,
            filename=name,
            mime=mime,
            size_bytes=len(data),
            uploaded_at=now,
            text=None,
            text_status=CvTextStatus.PENDING,
            text_error=None,
        )
        session.add(row)
    else:
        old_key = row.object_key
        row.object_key = key
        row.filename = name
        row.mime = mime
        row.size_bytes = len(data)
        row.uploaded_at = now
        row.text = None
        row.text_status = CvTextStatus.PENDING
        row.text_error = None
    await session.flush()
    if old_key is not None and old_key != key:
        await store.delete(old_key)
    await defer_job(session, jobs, JobName.CV_EXTRACT, user_id=str(user_id))
    return row


async def delete_cv(
    session: AsyncSession, jobs: procrastinate.App, store: ObjectStore, *, user_id: UUID
) -> None:
    await lock_user(session, user_id)
    row = await locked(session, models.CvFile, user_id)
    if row is None:
        return
    key = row.object_key
    await session.delete(row)
    await session.flush()
    await store.delete(key)
    await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(user_id))


async def cv_download(
    session: AsyncSession, store: ObjectStore, *, user_id: UUID
) -> tuple[models.CvFile, str]:
    row = await get_or_404(session, models.CvFile, user_id)
    url = await store.presign_get(row.object_key, CV_URL_TTL)
    return row, url


class ExtractionError(Exception):
    pass


def extract_text(mime: str, data: bytes) -> str:
    if mime == "application/pdf":
        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as error:
            raise ExtractionError(str(error)) from error
    if mime in {"text/markdown", "text/plain"}:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise ExtractionError("File is not valid UTF-8") from None
    raise ExtractionError("Unsupported file type")


async def write_cv_text(
    session: AsyncSession,
    jobs: procrastinate.App,
    *,
    user_id: UUID,
    object_key: str,
    text: str | None,
    status: CvTextStatus,
    error: str | None,
) -> bool:
    row = await locked(session, models.CvFile, user_id)
    if row is None or row.object_key != object_key:
        return False
    row.text = text
    row.text_status = status
    row.text_error = error
    await session.flush()
    await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(user_id))
    return True


async def extract_cv(runtime: Runtime, user_id: UUID) -> None:
    async with runtime.sessionmaker() as session:
        row = await session.get(models.CvFile, user_id)
        if row is None:
            return
        object_key = row.object_key
        mime = row.mime
        status = row.text_status
    if status == CvTextStatus.OK:
        return
    data = await require_storage(runtime.storage).get(object_key)
    try:
        text = extract_text(mime, data)
    except ExtractionError as error:
        async with runtime.sessionmaker.begin() as session:
            await write_cv_text(
                session,
                runtime.jobs,
                user_id=user_id,
                object_key=object_key,
                text=None,
                status=CvTextStatus.FAILED,
                error=str(error)[:1000],
            )
        return
    async with runtime.sessionmaker.begin() as session:
        await write_cv_text(
            session,
            runtime.jobs,
            user_id=user_id,
            object_key=object_key,
            text=text[:100_000],
            status=CvTextStatus.OK,
            error=None,
        )
