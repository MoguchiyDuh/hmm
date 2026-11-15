from uuid import UUID

from hmm_core.formation.runs import apply_run, fail_run, plan_formation, start_run
from hmm_core.platform.jobs import JobName, Runtime, job


@job(JobName.RUN_FORMATION)
async def run_formation(runtime: Runtime, event_id: str, run_id: str | None = None) -> None:
    event_uuid = UUID(event_id)
    run_uuid = UUID(run_id) if run_id else None
    try:
        async with runtime.sessionmaker.begin() as session:
            started = await start_run(
                session, event_uuid, runtime.algorithm.version, runtime.clock.now(), run_uuid
            )
            if started is None:
                return None
            row, request, units, baseline = started
            run_uuid = row.id
        packing, errors, output = await plan_formation(runtime, request, units, baseline)
        async with runtime.sessionmaker.begin() as session:
            await apply_run(
                session, event_uuid, run_uuid, packing, errors, output, runtime.clock.now()
            )
    except Exception as error:
        async with runtime.sessionmaker.begin() as session:
            await fail_run(session, event_uuid, run_uuid, error, runtime.clock.now())
        runtime.log.warning("Formation failed for %s", event_id, exc_info=error)
