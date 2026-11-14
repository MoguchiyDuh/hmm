from uuid import UUID

from hmm_core.platform.jobs import JobName, Runtime, job
from hmm_core.profiles import service as profiles


@job(JobName.CV_EXTRACT)
async def cv_extract(runtime: Runtime, user_id: str) -> None:
    await profiles.extract_cv(runtime, UUID(user_id))


@job(JobName.GITHUB_SYNC)
async def github_sync(runtime: Runtime, user_id: str) -> None:
    await profiles.sync_github(runtime, UUID(user_id))


@job(JobName.GITHUB_SYNC_ALL)
async def github_sync_all(runtime: Runtime) -> None:
    await profiles.sync_all_github(runtime)
