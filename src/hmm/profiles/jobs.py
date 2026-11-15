from uuid import UUID

from hmm.platform.jobs import JobName, Runtime, job
from hmm.profiles import cv as profiles_cv
from hmm.profiles import github as profiles_github


@job(JobName.CV_EXTRACT)
async def cv_extract(runtime: Runtime, user_id: str) -> None:
    await profiles_cv.extract_cv(runtime, UUID(user_id))


@job(JobName.GITHUB_SYNC)
async def github_sync(runtime: Runtime, user_id: str) -> None:
    await profiles_github.sync_github(runtime, UUID(user_id))


@job(JobName.GITHUB_SYNC_ALL)
async def github_sync_all(runtime: Runtime) -> None:
    await profiles_github.sync_all_github(runtime)
