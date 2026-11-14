from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    ErrorCode,
    EventStatus,
    FormationPhase,
    JoinedVia,
    LeftReason,
    MemberRole,
    MergeProposalStatus,
    MergeProposedBy,
    ProjectStatus,
    SwapRequestStatus,
    TeamStatus,
)
from hmm_contracts.notifications import (
    ActionKey,
    ApiAction,
    MergeProposedPayload,
    MergeResultPayload,
    NotificationPerson,
    SwapRequestReceivedPayload,
    SwapResultPayload,
)
from sqlalchemy import func, or_, select
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
from hmm_core.formation.lead_offers import close_pending_lead_offers
from hmm_core.formation.leads import close_project_pending_items, formation_sizes
from hmm_core.formation.members import note_lead_action
from hmm_core.notifications.service import notify, notify_many, resolve_alerts
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.settings import load_settings


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


async def _merge_counts(session: AsyncSession, project_ids: Sequence[UUID]) -> dict[UUID, int]:
    return dict(
        (project_id, count)
        for project_id, count in await session.execute(
            select(models.ProjectMember.project_id, func.count())
            .where(
                models.ProjectMember.project_id.in_(list(project_ids)),
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
            )
            .group_by(models.ProjectMember.project_id)
        )
    )


async def _merge_members(session: AsyncSession, project_id: UUID) -> tuple[NotificationPerson, ...]:
    return tuple(
        notification_person(user)
        for user in await session.scalars(
            select(models.User)
            .where(
                models.User.id.in_(
                    select(models.ProjectMember.user_id).where(
                        models.ProjectMember.project_id == project_id,
                        models.ProjectMember.left_at.is_(None),
                        models.ProjectMember.is_team_row.is_(True),
                    )
                )
            )
            .order_by(models.User.display_name, models.User.id)
        )
    )


async def notify_merge_result(
    session: AsyncSession,
    merge: models.MergeProposal,
    status: MergeProposalStatus,
    now: datetime,
    *,
    reason: str,
    include_members: bool,
) -> None:
    event = await session.get_one(models.Event, merge.event_id)
    absorbed = await session.get_one(models.Project, merge.absorbed_team_id)
    target = await session.get_one(models.Project, merge.target_team_id)
    target_lead = (
        await session.get(models.User, target.lead_user_id)
        if target.lead_user_id is not None
        else None
    )
    absorbed_lead = (
        await session.get(models.User, absorbed.lead_user_id)
        if absorbed.lead_user_id is not None
        else None
    )
    person = target_lead or absorbed_lead
    target_person = (
        notification_person(person)
        if person is not None
        else NotificationPerson(user_id=target.id, display_name=target.name)
    )
    members = await _merge_members(session, target.id)
    counts = await _merge_counts(session, [absorbed.id, target.id])
    payload = MergeResultPayload(
        link=f"/projects/{target.id}",
        event_id=event.id,
        event_title=event.title,
        project_id=target.id,
        team_name=target.name,
        merge_proposal_id=merge.id,
        target_project_id=target.id,
        target_team_name=target.name,
        target_lead=target_person,
        members=members,
        merged_size=counts.get(absorbed.id, 0) + counts.get(target.id, 0),
        status=status,
        reason=reason,
    )
    if include_members:
        ids = list(
            await session.scalars(
                select(models.ProjectMember.user_id).where(
                    models.ProjectMember.project_id.in_([absorbed.id, target.id]),
                    models.ProjectMember.left_at.is_(None),
                    models.ProjectMember.is_team_row.is_(True),
                )
            )
        )
    else:
        ids = [uid for uid in {absorbed.lead_user_id, target.lead_user_id} if uid is not None]
    await notify_many(session, ids, payload, now, dedupe_key=f"merge_result:{merge.id}")


async def expire_merge(
    session: AsyncSession,
    merge: models.MergeProposal,
    now: datetime,
    *,
    reason: str,
    status: MergeProposalStatus = MergeProposalStatus.EXPIRED,
) -> None:
    if MergeProposalStatus(merge.status) != MergeProposalStatus.PENDING:
        return
    merge.status = status.value
    await session.flush()
    await notify_merge_result(session, merge, status, now, reason=reason, include_members=False)


async def create_merge(
    session: AsyncSession,
    project_id: UUID,
    body: schemas.CreateMergeBody,
    actor: models.User,
    now: datetime,
) -> models.MergeProposal:
    absorbed, event, formation = await negotiation_context(session, project_id, actor)
    if event.status != EventStatus.PUBLISHED.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This hackathon is closed")
    if absorbed.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if absorbed.team_status != TeamStatus.REVIEW.value:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Merges are only available while both teams are in review"
        )
    if body.target_team_id == absorbed.id:
        raise ProblemError(ErrorCode.INVALID_STATE, "Choose another team")
    target = await session.get(models.Project, body.target_team_id)
    if target is None or target.event_id != event.id:
        raise ProblemError(ErrorCode.NOT_FOUND, "The target team is not in this hackathon")
    if target.team_status != TeamStatus.REVIEW.value:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Merges are only available while both teams are in review"
        )
    if target.lead_user_id is None:
        raise ProblemError(ErrorCode.INVALID_STATE, "The target team has no lead")
    sizes = formation_sizes(event, formation, await load_settings(session))
    project_ids = [absorbed.id, target.id]
    counts = await _merge_counts(session, project_ids)
    total = counts.get(absorbed.id, 0) + counts.get(target.id, 0)
    if total > sizes.max_size:
        raise ProblemError(
            ErrorCode.MERGE_TOO_LARGE,
            f"{total} members exceed the team maximum of {sizes.max_size}",
        )
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
            ErrorCode.INVALID_STATE, "A merge proposal is already pending for one of these teams"
        )
    if (
        await session.scalar(
            select(models.SwapRequest.id).where(
                models.SwapRequest.status == SwapRequestStatus.PENDING.value,
                or_(
                    models.SwapRequest.from_team_id.in_(project_ids),
                    models.SwapRequest.to_team_id.in_(project_ids),
                ),
            )
        )
        is not None
    ):
        raise ProblemError(ErrorCode.INVALID_STATE, "A swap is pending for one of these teams")
    merge = models.MergeProposal(
        event_id=event.id,
        run_id=None,
        absorbed_team_id=absorbed.id,
        target_team_id=target.id,
        proposed_by=MergeProposedBy.LEAD.value,
        absorbed_accepted_at=None,
        target_accepted_at=None,
        status=MergeProposalStatus.PENDING.value,
    )
    session.add(merge)
    await session.flush()
    await note_lead_action(session, absorbed, actor, now)
    expires_at = await negotiation_deadline(session, event, project_ids)
    target_lead = await session.get_one(models.User, target.lead_user_id)
    members = await _merge_members(session, target.id)
    actions = (
        ApiAction(key=ActionKey.MERGE_ACCEPT, entity_id=merge.id, label_key=ACCEPT_LABEL),
        ApiAction(key=ActionKey.MERGE_DECLINE, entity_id=merge.id, label_key=DECLINE_LABEL),
    )
    for project in (absorbed, target):
        lead_id = project.lead_user_id
        if lead_id is None:
            continue
        await notify(
            session,
            lead_id,
            MergeProposedPayload(
                project_id=project.id,
                team_name=project.name,
                link=f"/projects/{project.id}",
                target_project_id=target.id,
                target_team_name=target.name,
                target_lead=notification_person(target_lead),
                members=members,
                merged_size=total,
                merge_proposal_id=merge.id,
                expires_at=expires_at,
                reason=f"Merge proposed by {actor.display_name}",
                event_id=event.id,
                event_title=event.title,
                actions=actions,
            ),
            now,
            dedupe_key=f"merge_proposed:{merge.id}:{lead_id}",
        )
    await session.flush()
    await session.refresh(merge)
    return merge


async def _locked_merge(
    session: AsyncSession,
    merge_id: UUID,
    actor: models.User,
) -> tuple[
    models.MergeProposal, models.Event, models.EventFormation, models.Project, models.Project
]:
    merge = await session.get(models.MergeProposal, merge_id)
    if merge is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    event = await locked_formation_event(session, merge.event_id, actor)
    if not event.formation_enabled:
        raise ProblemError(ErrorCode.NOT_FOUND)
    formation = await formation_row(session, event.id)
    if FormationPhase(formation.phase) == FormationPhase.FORMING:
        raise ProblemError(ErrorCode.FORMATION_IN_PROGRESS, "Formation is running, retry shortly")
    merge = await session.scalar(
        select(models.MergeProposal)
        .where(models.MergeProposal.id == merge_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if merge is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    projects: dict[UUID, models.Project] = {}
    for project_id in sorted([merge.absorbed_team_id, merge.target_team_id]):
        project = await session.scalar(
            select(models.Project)
            .where(models.Project.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if project is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        projects[project_id] = project
    absorbed, target = projects[merge.absorbed_team_id], projects[merge.target_team_id]
    if actor.id not in {absorbed.lead_user_id, target.lead_user_id}:
        raise ProblemError(ErrorCode.FORBIDDEN)
    return merge, event, formation, absorbed, target


async def accept_merge(
    session: AsyncSession,
    jobs: procrastinate.App,
    merge_id: UUID,
    actor: models.User,
    now: datetime,
) -> models.MergeProposal | ProblemError:
    merge, event, formation, absorbed, target = await _locked_merge(session, merge_id, actor)
    status = MergeProposalStatus(merge.status)
    if status == MergeProposalStatus.ACCEPTED:
        await session.refresh(merge)
        return merge
    if status != MergeProposalStatus.PENDING:
        raise ProblemError(ErrorCode.INVALID_STATE, "This merge proposal is no longer pending")
    if now >= await negotiation_deadline(session, event, [absorbed.id, target.id]):
        detail = "The merge deadline has passed"
        await expire_merge(session, merge, now, reason=detail)
        return ProblemError(ErrorCode.INVALID_STATE, detail)
    if absorbed.lead_user_id == actor.id and merge.absorbed_accepted_at is None:
        merge.absorbed_accepted_at = now
    if target.lead_user_id == actor.id and merge.target_accepted_at is None:
        merge.target_accepted_at = now
    await note_lead_action(session, absorbed, actor, now)
    await note_lead_action(session, target, actor, now)
    if merge.absorbed_accepted_at is not None and merge.target_accepted_at is not None:
        error = await apply_merge(session, jobs, merge, event, formation, absorbed, target, now)
        if error is not None:
            return error
    await session.flush()
    await session.refresh(merge)
    return merge


async def apply_merge(
    session: AsyncSession,
    jobs: procrastinate.App,
    merge: models.MergeProposal,
    event: models.Event,
    formation: models.EventFormation,
    absorbed: models.Project,
    target: models.Project,
    now: datetime,
) -> ProblemError | None:
    detail: str | None = None
    if (
        absorbed.team_status != TeamStatus.REVIEW.value
        or target.team_status != TeamStatus.REVIEW.value
        or absorbed.event_id != event.id
        or target.event_id != event.id
    ):
        detail = "The teams are no longer in review"
    elif target.lead_user_id is None:
        detail = "The target team has no lead"
    else:
        counts = await _merge_counts(session, [absorbed.id, target.id])
        total = counts.get(absorbed.id, 0) + counts.get(target.id, 0)
        sizes = formation_sizes(event, formation, await load_settings(session))
        if total > sizes.max_size:
            detail = f"{total} members exceed the team maximum of {sizes.max_size}"
    if detail is not None:
        await expire_merge(session, merge, now, reason=detail, status=MergeProposalStatus.CLOSED)
        return ProblemError(ErrorCode.INVALID_STATE, detail)
    absorbed_rows = list(
        await session.scalars(
            select(models.ProjectMember)
            .where(
                models.ProjectMember.project_id == absorbed.id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
            )
            .order_by(models.ProjectMember.joined_at, models.ProjectMember.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    previous_lead = absorbed.lead_user_id
    member_count = len(absorbed_rows)
    for row in absorbed_rows:
        row.left_at = now
        row.left_reason = LeftReason.MERGED.value
    await session.flush()
    for row in absorbed_rows:
        session.add(
            models.ProjectMember(
                project_id=target.id,
                event_id=event.id,
                user_id=row.user_id,
                role=MemberRole.MEMBER.value,
                area_id=row.area_id,
                state=row.state,
                trust=row.trust,
                joined_via=JoinedVia.MERGE.value,
                proposal_reason=row.proposal_reason,
                confirm_by=row.confirm_by,
                confirm_kind=row.confirm_kind,
                is_team_row=True,
                joined_at=now,
                confirmed_at=row.confirmed_at,
            )
        )
    absorbed.team_status = TeamStatus.DISBANDED.value
    absorbed.status = ProjectStatus.DISBANDED.value
    absorbed.lead_user_id = None
    absorbed.ready_at = None
    absorbed.last_lead_action_at = now
    await close_pending_lead_offers(session, absorbed.id, now)
    await close_project_pending_items(session, absorbed.id, now, reason="team_disbanded")
    merge.status = MergeProposalStatus.ACCEPTED.value
    await session.flush()
    if previous_lead is not None:
        await record_audit(
            session,
            actor=None,
            actor_kind=ActorKind.SYSTEM,
            action=AuditAction.LEAD_STEP_DOWN,
            entity_type=AuditEntityType.PROJECT,
            entity_id=absorbed.id,
            before={"lead_user_id": str(previous_lead)},
            after={"lead_user_id": None},
            reason="The team was absorbed by a merge",
        )
    await record_audit(
        session,
        actor=None,
        actor_kind=ActorKind.SYSTEM,
        action=AuditAction.SYSTEM_TRANSITION,
        entity_type=AuditEntityType.PROJECT,
        entity_id=absorbed.id,
        before={"team_status": TeamStatus.REVIEW.value, "members": member_count},
        after={
            "team_status": TeamStatus.DISBANDED.value,
            "merge_proposal_id": str(merge.id),
            "merged_into": str(target.id),
        },
        reason=None,
    )
    await resolve_alerts(
        session,
        f"formation_undersized:{absorbed.id}:{formation.rounds}",
        now,
        note="The team was absorbed by a merge",
    )
    await defer_job(session, jobs, JobName.REFRESH_RECOMMENDATIONS, project_id=str(target.id))
    await notify_merge_result(
        session,
        merge,
        MergeProposalStatus.ACCEPTED,
        now,
        reason="The teams merged",
        include_members=True,
    )
    return None


async def decline_merge(
    session: AsyncSession,
    merge_id: UUID,
    actor: models.User,
    now: datetime,
) -> models.MergeProposal:
    merge, _, _, absorbed, target = await _locked_merge(session, merge_id, actor)
    status = MergeProposalStatus(merge.status)
    if status == MergeProposalStatus.DECLINED:
        await session.refresh(merge)
        return merge
    if status != MergeProposalStatus.PENDING:
        raise ProblemError(ErrorCode.INVALID_STATE, "This merge proposal is no longer pending")
    merge.status = MergeProposalStatus.DECLINED.value
    await session.flush()
    await notify_merge_result(
        session,
        merge,
        MergeProposalStatus.DECLINED,
        now,
        reason="The merge was declined",
        include_members=False,
    )
    await note_lead_action(session, absorbed, actor, now)
    await note_lead_action(session, target, actor, now)
    await session.flush()
    await session.refresh(merge)
    return merge


async def merge_view(
    session: AsyncSession,
    merge: models.MergeProposal,
    actor: models.User,
    now: datetime,
) -> schemas.Merge:
    absorbed = await session.get_one(models.Project, merge.absorbed_team_id)
    target = await session.get_one(models.Project, merge.target_team_id)
    event = await session.get_one(models.Event, merge.event_id)
    lead = actor.id in {absorbed.lead_user_id, target.lead_user_id}
    expires_at = await negotiation_deadline(session, event, [absorbed.id, target.id])
    actions = (
        [schemas.MergeAction.ACCEPT, schemas.MergeAction.DECLINE]
        if (
            MergeProposalStatus(merge.status) == MergeProposalStatus.PENDING
            and lead
            and now < expires_at
        )
        else []
    )
    return schemas.Merge(
        id=merge.id,
        event_id=merge.event_id,
        run_id=merge.run_id,
        absorbed_team_id=merge.absorbed_team_id,
        target_team_id=merge.target_team_id,
        proposed_by=MergeProposedBy(merge.proposed_by),
        absorbed_accepted_at=merge.absorbed_accepted_at,
        target_accepted_at=merge.target_accepted_at,
        status=MergeProposalStatus(merge.status),
        created_at=merge.created_at,
        updated_at=merge.updated_at,
        allowed_actions=actions,
    )
