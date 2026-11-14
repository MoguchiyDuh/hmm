from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    ErrorCode,
    EventStatus,
    FormationPhase,
    JoinedVia,
    LeftReason,
    MemberRole,
    MergeProposalStatus,
    SwapRequestStatus,
    TeamStatus,
)
from hmm_contracts.notifications import (
    ActionKey,
    ApiAction,
    SwapRequestReceivedPayload,
    SwapResultPayload,
)
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.formation import schemas
from hmm_core.formation.common import (
    ACCEPT_LABEL,
    DECLINE_LABEL,
    formation_row,
    locked_formation_event,
    notification_person,
)
from hmm_core.formation.enrollments import live_membership
from hmm_core.formation.members import note_lead_action
from hmm_core.notifications.service import notify, notify_many
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job


async def negotiation_context(
    session: AsyncSession, project_id: UUID, actor: models.User
) -> tuple[models.Project, models.Event, models.EventFormation]:
    project = await session.get(models.Project, project_id)
    if project is None or project.event_id is None or project.team_status is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    event = await locked_formation_event(session, project.event_id, actor)
    if not event.formation_enabled:
        raise ProblemError(ErrorCode.NOT_FOUND)
    formation = await formation_row(session, event.id)
    if FormationPhase(formation.phase) == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    project = await session.scalar(
        select(models.Project)
        .where(models.Project.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return project, event, formation


async def negotiation_deadline(
    session: AsyncSession, event: models.Event, project_ids: Sequence[UUID]
) -> datetime:
    formation = await formation_row(session, event.id)
    default = formation.review_ends_at or event.starts_at
    rows = list(
        await session.scalars(
            select(models.Project.review_ends_at).where(models.Project.id.in_(list(project_ids)))
        )
    )
    return min([value or default for value in rows] + [default])


async def notify_swap_result(
    session: AsyncSession,
    swap: models.SwapRequest,
    status: SwapRequestStatus,
    now: datetime,
    *,
    reason: str,
) -> None:
    event = await session.get_one(models.Event, swap.event_id)
    from_project = await session.get_one(models.Project, swap.from_team_id)
    to_project = await session.get_one(models.Project, swap.to_team_id)
    users = {
        row.id: row
        for row in await session.scalars(
            select(models.User).where(
                models.User.id.in_(
                    {swap.from_user_id, swap.to_user_id, swap.requested_by}
                    | {
                        user_id
                        for user_id in (from_project.lead_user_id, to_project.lead_user_id)
                        if user_id is not None
                    }
                )
            )
        )
    }
    for project, other, user_id in (
        (from_project, to_project, swap.from_user_id),
        (to_project, from_project, swap.to_user_id),
    ):
        payload = SwapResultPayload(
            project_id=project.id,
            team_name=project.name,
            link=f"/projects/{project.id}",
            other_project_id=other.id,
            other_team_name=other.name,
            event_id=event.id,
            event_title=event.title,
            swap_request_id=swap.id,
            from_member=notification_person(users[swap.from_user_id]),
            to_member=notification_person(users[swap.to_user_id]),
            status=status,
            reason=reason,
        )
        await notify_many(
            session,
            [uid for uid in {user_id, project.lead_user_id} if uid is not None],
            payload,
            now,
            dedupe_key=f"swap_result:{swap.id}",
        )


async def expire_swap(
    session: AsyncSession,
    swap: models.SwapRequest,
    now: datetime,
    *,
    reason: str,
    status: SwapRequestStatus = SwapRequestStatus.EXPIRED,
) -> None:
    if SwapRequestStatus(swap.status) != SwapRequestStatus.PENDING:
        return
    swap.status = status.value
    await session.flush()
    await notify_swap_result(session, swap, status, now, reason=reason)


async def create_swap(
    session: AsyncSession,
    project_id: UUID,
    body: schemas.CreateSwapBody,
    actor: models.User,
    now: datetime,
) -> models.SwapRequest:
    project, event, _ = await negotiation_context(session, project_id, actor)
    if event.status != EventStatus.PUBLISHED.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This hackathon is closed")
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if project.team_status != TeamStatus.REVIEW.value:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Swaps are only available while both teams are in review"
        )
    if (
        await session.scalar(
            select(models.ProjectMember.id).where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.user_id == body.from_user_id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
            )
        )
        is None
    ):
        raise ProblemError(ErrorCode.NOT_FOUND, "The member is not on this team")
    if body.from_user_id == project.lead_user_id:
        raise ProblemError(
            ErrorCode.LEAD_NOT_SWAPPABLE, "The lead must transfer the role before swapping"
        )
    target = await live_membership(session, event.id, body.to_user_id)
    if target is None:
        raise ProblemError(
            ErrorCode.NOT_FOUND, "The target member is not on a team in this hackathon"
        )
    to_project = target[1]
    if to_project.id == project.id:
        raise ProblemError(ErrorCode.INVALID_STATE, "Choose a member of another team")
    if to_project.team_status != TeamStatus.REVIEW.value:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Swaps are only available while both teams are in review"
        )
    if body.to_user_id == to_project.lead_user_id:
        raise ProblemError(
            ErrorCode.LEAD_NOT_SWAPPABLE, "The lead must transfer the role before swapping"
        )
    user_ids = [body.from_user_id, body.to_user_id]
    if (
        await session.scalar(
            select(models.SwapRequest.id).where(
                models.SwapRequest.status == SwapRequestStatus.PENDING.value,
                or_(
                    models.SwapRequest.from_user_id.in_(user_ids),
                    models.SwapRequest.to_user_id.in_(user_ids),
                ),
            )
        )
        is not None
    ):
        raise ProblemError(
            ErrorCode.INVALID_STATE, "A swap for one of these members is already pending"
        )
    project_ids = [project.id, to_project.id]
    if (
        await session.scalar(
            select(models.MergeProposal.id).where(
                models.MergeProposal.status == MergeProposalStatus.PENDING.value,
                or_(
                    models.MergeProposal.absorbed_team_id.in_(project_ids),
                    models.MergeProposal.target_team_id.in_(project_ids),
                ),
            )
        )
        is not None
    ):
        raise ProblemError(
            ErrorCode.INVALID_STATE, "A merge proposal is pending for one of these teams"
        )
    swap = models.SwapRequest(
        event_id=event.id,
        from_team_id=project.id,
        from_user_id=body.from_user_id,
        to_team_id=to_project.id,
        to_user_id=body.to_user_id,
        requested_by=actor.id,
        to_lead_accepted_at=None,
        from_user_accepted_at=None,
        to_user_accepted_at=None,
        status=SwapRequestStatus.PENDING.value,
    )
    session.add(swap)
    await session.flush()
    await note_lead_action(session, project, actor, now)
    expires_at = await negotiation_deadline(session, event, project_ids)
    from_user = await session.get_one(models.User, body.from_user_id)
    to_user = await session.get_one(models.User, body.to_user_id)
    actions = (
        ApiAction(key=ActionKey.SWAP_ACCEPT, entity_id=swap.id, label_key=ACCEPT_LABEL),
        ApiAction(key=ActionKey.SWAP_DECLINE, entity_id=swap.id, label_key=DECLINE_LABEL),
    )
    for user_id, own, other in (
        (to_project.lead_user_id, to_project, project),
        (body.from_user_id, project, to_project),
        (body.to_user_id, to_project, project),
    ):
        if user_id is None:
            continue
        await notify(
            session,
            user_id,
            SwapRequestReceivedPayload(
                project_id=own.id,
                team_name=own.name,
                link=f"/projects/{own.id}",
                other_project_id=other.id,
                other_team_name=other.name,
                event_id=event.id,
                event_title=event.title,
                swap_request_id=swap.id,
                from_member=notification_person(from_user),
                to_member=notification_person(to_user),
                expires_at=expires_at,
                actions=actions,
            ),
            now,
            dedupe_key=f"swap_request_received:{swap.id}:{user_id}",
        )
    await session.flush()
    await session.refresh(swap)
    return swap


async def _locked_swap(
    session: AsyncSession,
    swap_id: UUID,
    actor: models.User,
) -> tuple[models.SwapRequest, models.Event, models.Project, models.Project]:
    swap = await session.get(models.SwapRequest, swap_id)
    if swap is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    event = await locked_formation_event(session, swap.event_id, actor)
    if not event.formation_enabled:
        raise ProblemError(ErrorCode.NOT_FOUND)
    formation = await formation_row(session, event.id)
    if FormationPhase(formation.phase) == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    swap = await session.scalar(
        select(models.SwapRequest)
        .where(models.SwapRequest.id == swap_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if swap is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    projects: dict[UUID, models.Project] = {}
    for project_id in sorted([swap.from_team_id, swap.to_team_id]):
        project = await session.scalar(
            select(models.Project)
            .where(models.Project.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if project is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        projects[project_id] = project
    from_project, to_project = projects[swap.from_team_id], projects[swap.to_team_id]
    if actor.id not in {
        from_project.lead_user_id,
        to_project.lead_user_id,
        swap.from_user_id,
        swap.to_user_id,
    }:
        raise ProblemError(ErrorCode.FORBIDDEN)
    return swap, event, from_project, to_project


async def accept_swap(
    session: AsyncSession,
    jobs: procrastinate.App,
    swap_id: UUID,
    actor: models.User,
    now: datetime,
) -> models.SwapRequest | ProblemError:
    swap, event, from_project, to_project = await _locked_swap(session, swap_id, actor)
    status = SwapRequestStatus(swap.status)
    if status == SwapRequestStatus.ACCEPTED:
        await session.refresh(swap)
        return swap
    if status != SwapRequestStatus.PENDING:
        raise ProblemError(ErrorCode.INVALID_STATE, "This swap is no longer pending")
    if now >= await negotiation_deadline(session, event, [from_project.id, to_project.id]):
        detail = "The swap deadline has passed"
        await expire_swap(session, swap, now, reason=detail)
        return ProblemError(ErrorCode.INVALID_STATE, detail)
    if to_project.lead_user_id == actor.id and swap.to_lead_accepted_at is None:
        swap.to_lead_accepted_at = now
    if swap.from_user_id == actor.id and swap.from_user_accepted_at is None:
        swap.from_user_accepted_at = now
    if swap.to_user_id == actor.id and swap.to_user_accepted_at is None:
        swap.to_user_accepted_at = now
    await note_lead_action(session, from_project, actor, now)
    await note_lead_action(session, to_project, actor, now)
    if all(
        stamp is not None
        for stamp in (
            swap.to_lead_accepted_at,
            swap.from_user_accepted_at,
            swap.to_user_accepted_at,
        )
    ):
        error = await apply_swap(session, jobs, swap, event, from_project, to_project, now)
        if error is not None:
            return error
    await session.flush()
    await session.refresh(swap)
    return swap


async def apply_swap(
    session: AsyncSession,
    jobs: procrastinate.App,
    swap: models.SwapRequest,
    event: models.Event,
    from_project: models.Project,
    to_project: models.Project,
    now: datetime,
) -> ProblemError | None:
    if (
        from_project.team_status != TeamStatus.REVIEW.value
        or to_project.team_status != TeamStatus.REVIEW.value
        or from_project.event_id != event.id
        or to_project.event_id != event.id
    ):
        detail = "The teams are no longer in review"
        await expire_swap(session, swap, now, reason=detail, status=SwapRequestStatus.CLOSED)
        return ProblemError(ErrorCode.INVALID_STATE, detail)
    rows: list[models.ProjectMember] = []
    for project_id, user_id in (
        (from_project.id, swap.from_user_id),
        (to_project.id, swap.to_user_id),
    ):
        row = await session.scalar(
            select(models.ProjectMember)
            .where(
                models.ProjectMember.project_id == project_id,
                models.ProjectMember.user_id == user_id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            detail = "The participants changed"
            await expire_swap(session, swap, now, reason=detail, status=SwapRequestStatus.CLOSED)
            return ProblemError(ErrorCode.INVALID_STATE, detail)
        rows.append(row)
    if from_project.lead_user_id == swap.from_user_id or to_project.lead_user_id == swap.to_user_id:
        raise ProblemError(
            ErrorCode.LEAD_NOT_SWAPPABLE, "The lead must transfer the role before swapping"
        )
    for row in rows:
        row.left_at = now
        row.left_reason = LeftReason.SWAPPED.value
    await session.flush()
    for row, project in zip(rows, (to_project, from_project), strict=True):
        session.add(
            models.ProjectMember(
                project_id=project.id,
                event_id=event.id,
                user_id=row.user_id,
                role=MemberRole.MEMBER.value,
                area_id=row.area_id,
                state=row.state,
                trust=row.trust,
                joined_via=JoinedVia.SWAP.value,
                proposal_reason=row.proposal_reason,
                confirm_by=row.confirm_by,
                confirm_kind=row.confirm_kind,
                is_team_row=True,
                joined_at=now,
                confirmed_at=row.confirmed_at,
            )
        )
    swap.status = SwapRequestStatus.ACCEPTED.value
    await session.flush()
    for project in (from_project, to_project):
        await defer_job(session, jobs, JobName.REFRESH_RECOMMENDATIONS, project_id=str(project.id))
    await notify_swap_result(
        session, swap, SwapRequestStatus.ACCEPTED, now, reason="The swap was accepted"
    )
    return None


async def decline_swap(
    session: AsyncSession,
    swap_id: UUID,
    actor: models.User,
    now: datetime,
) -> models.SwapRequest:
    swap, _, from_project, to_project = await _locked_swap(session, swap_id, actor)
    status = SwapRequestStatus(swap.status)
    if status == SwapRequestStatus.DECLINED:
        await session.refresh(swap)
        return swap
    if status != SwapRequestStatus.PENDING:
        raise ProblemError(ErrorCode.INVALID_STATE, "This swap is no longer pending")
    swap.status = SwapRequestStatus.DECLINED.value
    await session.flush()
    await notify_swap_result(
        session, swap, SwapRequestStatus.DECLINED, now, reason="The swap was declined"
    )
    await note_lead_action(session, from_project, actor, now)
    await note_lead_action(session, to_project, actor, now)
    await session.flush()
    await session.refresh(swap)
    return swap


async def swap_view(
    session: AsyncSession,
    swap: models.SwapRequest,
    actor: models.User,
    now: datetime,
) -> schemas.Swap:
    from_project = await session.get_one(models.Project, swap.from_team_id)
    to_project = await session.get_one(models.Project, swap.to_team_id)
    participant = actor.id in {
        swap.from_user_id,
        swap.to_user_id,
        from_project.lead_user_id,
        to_project.lead_user_id,
    }
    expires_at = await negotiation_deadline(
        session,
        await session.get_one(models.Event, swap.event_id),
        [from_project.id, to_project.id],
    )
    actions = (
        [schemas.SwapAction.ACCEPT, schemas.SwapAction.DECLINE]
        if (
            SwapRequestStatus(swap.status) == SwapRequestStatus.PENDING
            and participant
            and now < expires_at
        )
        else []
    )
    return schemas.Swap(
        id=swap.id,
        event_id=swap.event_id,
        from_team_id=swap.from_team_id,
        from_user_id=swap.from_user_id,
        to_team_id=swap.to_team_id,
        to_user_id=swap.to_user_id,
        requested_by=swap.requested_by,
        to_lead_accepted_at=swap.to_lead_accepted_at,
        from_user_accepted_at=swap.from_user_accepted_at,
        to_user_accepted_at=swap.to_user_accepted_at,
        status=SwapRequestStatus(swap.status),
        created_at=swap.created_at,
        updated_at=swap.updated_at,
        allowed_actions=actions,
    )
