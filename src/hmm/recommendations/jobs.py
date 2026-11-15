from uuid import UUID

from hmm import models
from hmm.notifications.payloads import AlertKind
from hmm.notifications.service import raise_alert
from hmm.platform.algo import call_algo
from hmm.platform.jobs import JobName, Runtime, job
from hmm.recommendations import service


@job(JobName.REFRESH_RECOMMENDATIONS)
async def refresh_recommendations(runtime: Runtime, project_id: str) -> None:
    now = runtime.clock.now()
    project_uuid = UUID(project_id)
    async with runtime.sessionmaker.begin() as session:
        project = await session.get(models.Project, project_uuid)
        if project is None:
            return None
        name = project.name
        prepared = await service.prepare_refresh(session, project, now, runtime.algorithm.version)
        if prepared is None:
            return None
        row_id, context = prepared
    async with runtime.sessionmaker() as session:
        ctx = await runtime.algo_context(session)
    try:
        result = await call_algo("recommend", runtime.algorithm.recommend(ctx, context.request))
    except Exception as error:
        failure_now = runtime.clock.now()
        async with runtime.sessionmaker.begin() as session:
            await service.mark_failed(session, row_id, error, failure_now)
            await raise_alert(
                session,
                AlertKind.ALGO_FAILURE,
                failure_now,
                title="Recommendation refresh failed",
                message=f"Quick-pick for {name} failed: {type(error).__name__}",
                link=f"/projects/{project_uuid}",
                subject_type="project",
                subject_id=project_uuid,
                subject_title=name,
                dedupe_key=(
                    f"algo_failure:recommend:{project_uuid}:{failure_now.strftime('%Y-%m-%d-%H')}"
                ),
            )
        raise
    validated, dropped = service.validated_recommendations(
        result, context.enrolled_ids, context.others_ids
    )
    if dropped:
        runtime.log.warning(
            "Dropped %d invalid recommendation item(s) for project %s", dropped, project_uuid
        )
    completion_now = runtime.clock.now()
    async with runtime.sessionmaker.begin() as session:
        await service.mark_ready(session, row_id, validated, completion_now)
    return None
