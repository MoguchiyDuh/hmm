from uuid import UUID

from hmm_core.platform.jobs import JobName, Runtime, job
from hmm_core.scores import recompute


@job(JobName.RECOMPUTE_MEMBER)
async def recompute_member(runtime: Runtime, user_id: str) -> None:
    async with runtime.sessionmaker.begin() as session:
        await recompute.recompute_member(session, runtime, UUID(user_id))


@job(JobName.RECOMPUTE_ALL_MEMBERS)
async def recompute_all_members(runtime: Runtime) -> None:
    async with runtime.sessionmaker.begin() as session:
        await recompute.recompute_all_members(session, runtime)
