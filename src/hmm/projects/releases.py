from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

import procrastinate
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.common import (
    ConfirmationFactType,
    ConfirmationSource,
    ErrorCode,
    MemberState,
    ProjectStatus,
    ReviewWindowSubjectType,
)
from hmm.feed import confirmations as feed_confirmations
from hmm.feed import digests as feed_digests
from hmm.feed import timeline as feed_timeline
from hmm.notifications.payloads import ProjectReleasedPayload
from hmm.notifications.service import notify_many
from hmm.platform.db import get_or_404
from hmm.platform.errors import ProblemError
from hmm.platform.jobs import JobName, defer_job
from hmm.platform.schemas import notification_person
from hmm.projects import schemas
from hmm.projects.common import visible_project
from hmm.projects.lifecycle import (
    _sync_recruiting,
    resolve_project_areas,
)
from hmm.projects.views import project_closed
from hmm.results import windows as results_windows

CONTRIBUTION_MEMBER_WINDOW = timedelta(days=30)
RELEASE_WINDOW_REPEAT = timedelta(days=30)


async def release_views(
    session: AsyncSession, releases: Sequence[models.ProjectRelease]
) -> list[schemas.ProjectRelease]:
    if not releases:
        return []
    contributions: dict[UUID, list[schemas.ReleaseContribution]] = {
        release.id: [] for release in releases
    }
    rows = await session.execute(
        select(models.ReleaseContribution, models.Area.key)
        .outerjoin(models.Area, models.Area.id == models.ReleaseContribution.area_id)
        .where(models.ReleaseContribution.release_id.in_([release.id for release in releases]))
        .order_by(models.ReleaseContribution.user_id)
    )
    for contribution, area_key in rows:
        contributions[contribution.release_id].append(
            schemas.ReleaseContribution(
                user_id=contribution.user_id,
                contributed=contribution.contributed,
                area_key=area_key,
            )
        )
    return [
        schemas.ProjectRelease.model_validate(
            {
                "id": release.id,
                "project_id": release.project_id,
                "title": release.title,
                "notes_md": release.notes_md,
                "repo_url": release.repo_url,
                "demo_url": release.demo_url,
                "writeup_url": release.writeup_url,
                "contributions": contributions[release.id],
                "created_by": release.created_by,
                "created_at": release.created_at,
            }
        )
        for release in releases
    ]


async def list_releases(
    session: AsyncSession, project_id: UUID, actor: models.User
) -> list[schemas.ProjectRelease]:
    project = await visible_project(session, project_id, actor)
    releases = await session.scalars(
        select(models.ProjectRelease)
        .where(models.ProjectRelease.project_id == project.id)
        .order_by(models.ProjectRelease.created_at.desc(), models.ProjectRelease.id.desc())
    )
    return await release_views(session, list(releases))


async def create_release(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    body: schemas.CreateProjectReleaseBody,
    actor: models.User,
    now: datetime,
) -> schemas.ProjectRelease:

    project = await get_or_404(session, models.Project, project_id, lock=True)
    if project_closed(project):
        raise ProblemError(ErrorCode.INVALID_STATE, "This project is closed")
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    cutoff = now - CONTRIBUTION_MEMBER_WINDOW
    rows = list(
        await session.scalars(
            select(models.ProjectMember)
            .where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.state == MemberState.CONFIRMED.value,
                or_(
                    models.ProjectMember.left_at.is_(None),
                    models.ProjectMember.left_at > cutoff,
                ),
            )
            .order_by(models.ProjectMember.user_id)
            .with_for_update()
        )
    )
    by_user = {contribution.user_id: contribution for contribution in body.contributions}
    if len(by_user) != len(body.contributions):
        raise ProblemError(ErrorCode.VALIDATION_ERROR, "Duplicate contribution for user")
    if set(by_user) != {row.user_id for row in rows}:
        raise ProblemError(
            ErrorCode.VALIDATION_ERROR,
            "contributions must cover every current member and members who left in the last "
            "30 days",
        )
    keys = list(
        dict.fromkeys(
            contribution.area_key
            for contribution in body.contributions
            if contribution.area_key is not None
        )
    )
    area_ids = await resolve_project_areas(session, keys)
    area_by_key = dict(zip(keys, area_ids, strict=True))
    release = models.ProjectRelease(
        project_id=project.id,
        title=body.title,
        notes_md=body.notes_md,
        repo_url=str(body.repo_url) if body.repo_url is not None else None,
        demo_url=str(body.demo_url) if body.demo_url is not None else None,
        writeup_url=str(body.writeup_url) if body.writeup_url is not None else None,
        created_by=actor.id,
    )
    session.add(release)
    await session.flush()
    for row in rows:
        contribution = by_user[row.user_id]
        session.add(
            models.ReleaseContribution(
                release_id=release.id,
                user_id=row.user_id,
                contributed=contribution.contributed,
                area_id=area_by_key.get(contribution.area_key),
            )
        )
    for row in rows:
        if by_user[row.user_id].contributed and row.user_id != actor.id:
            await feed_confirmations.add_confirmation(
                session,
                jobs,
                fact_type=ConfirmationFactType.PROJECT_MEMBER,
                fact_id=row.id,
                subject_user_id=row.user_id,
                confirmer_id=actor.id,
                source=ConfirmationSource.LEAD,
                now=now,
            )
    if project.status in (
        ProjectStatus.IDEA.value,
        ProjectStatus.ACTIVE.value,
        ProjectStatus.PAUSED.value,
    ):
        project.status = ProjectStatus.SHIPPED.value
    project.paused_reason = None
    project.last_progress_at = now
    await session.flush()
    await _sync_recruiting(session, project, actor.id, now)
    await feed_timeline.record_project_shipped(session, project, release, actor.id, now)
    members = [row.user_id for row in rows if row.left_at is None and row.user_id != actor.id]
    await notify_many(
        session,
        members,
        ProjectReleasedPayload(
            link=f"/projects/{project.id}",
            project_id=project.id,
            project_name=project.name,
            release_id=release.id,
            release_title=release.title,
            actor=notification_person(actor),
            notes_md=release.notes_md,
            repo_url=release.repo_url,
            demo_url=release.demo_url,
            writeup_url=release.writeup_url,
        ),
        now,
        dedupe_key=f"project_released:{release.id}",
    )
    await feed_digests.send_contributions_digest(session, release.id, now)
    last_opened = await session.scalar(
        select(func.max(models.ReviewWindow.opens_at)).where(
            models.ReviewWindow.subject_type == ReviewWindowSubjectType.PROJECT.value,
            models.ReviewWindow.subject_id == project.id,
        )
    )
    if last_opened is None or last_opened <= now - RELEASE_WINDOW_REPEAT:
        participants = await results_windows.release_review_participants(
            session, project_id=project.id, now=now
        )
        await results_windows.open_review_window(
            session,
            subject_type=ReviewWindowSubjectType.PROJECT,
            subject_id=project.id,
            participants=participants,
            now=now,
        )
    for row in rows:
        await defer_job(session, jobs, JobName.RECOMPUTE_MEMBER, user_id=str(row.user_id))
    await session.flush()
    return (await release_views(session, [release]))[0]
