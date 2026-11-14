from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import procrastinate
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    ErrorCode,
    LeadOfferSource,
    LeadOfferStatus,
    MemberRole,
    MemberState,
    TeamStatus,
)
from hmm_contracts.notifications import (
    AlertKind,
    LeadAssignedPayload,
    LeadOfferDeclinedPayload,
    LeadOfferPayload,
    LeadTransferredPayload,
)
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.formation.common import (
    FORCEABLE_TEAM_STATUSES,
    LIVE_ENROLLMENT_STATES,
    RESULT_TEAM_STATUSES,
    locked_formation_event,
    notification_person,
)
from hmm_core.formation.leads import lead_candidate_tier
from hmm_core.notifications.service import notify, notify_many, raise_alert
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.jobs import JobName, defer_job
from hmm_core.platform.settings import load_settings
from hmm_core.projects import schemas as project_schemas
from hmm_core.projects import service as projects


@dataclass(frozen=True, slots=True)
class LeadOrderEntry:
    member: models.ProjectMember
    user: models.User
    fit: float
    tier: int
    enrolled_at: datetime | None


async def lead_order(
    session: AsyncSession, project: models.Project, now: datetime
) -> list[LeadOrderEntry]:
    if project.event_id is None:
        return []
    rows = (
        await session.execute(
            select(models.ProjectMember, models.User)
            .join(models.User, models.User.id == models.ProjectMember.user_id)
            .where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
                models.User.banned_at.is_(None),
            )
        )
    ).all()
    if not rows:
        return []
    user_ids = {user.id for _, user in rows}
    enrollments: dict[UUID, models.Enrollment] = {
        enrollment.user_id: enrollment
        for enrollment in await session.scalars(
            select(models.Enrollment).where(
                models.Enrollment.event_id == project.event_id,
                models.Enrollment.user_id.in_(user_ids),
                models.Enrollment.status.in_(
                    sorted(member.value for member in LIVE_ENROLLMENT_STATES)
                ),
            )
        )
    }
    fits: dict[UUID, models.LeadFit] = {
        row.user_id: row
        for row in await session.scalars(
            select(models.LeadFit).where(
                models.LeadFit.event_id == project.event_id,
                models.LeadFit.user_id.in_(user_ids),
            )
        )
    }
    claimed = {project.lead_user_id} if project.lead_user_id is not None else set()
    settings = await load_settings(session)
    entries: list[LeadOrderEntry] = []
    for member, user in rows:
        row = fits.get(user.id)
        fit = float(row.fit) if row is not None else 0.0
        enrollment = enrollments.get(user.id)
        entries.append(
            LeadOrderEntry(
                member=member,
                user=user,
                fit=fit,
                tier=lead_candidate_tier(
                    user.id,
                    fit,
                    enrollment.willing_to_lead if enrollment else False,
                    claimed,
                    settings,
                ),
                enrolled_at=enrollment.created_at if enrollment else None,
            )
        )
    return sorted(
        entries,
        key=lambda entry: (
            entry.tier,
            -entry.fit,
            entry.enrolled_at is None,
            entry.enrolled_at or now,
            str(entry.user.id),
        ),
    )


async def team_member_ids(session: AsyncSession, project_id: UUID) -> list[UUID]:
    return list(
        await session.scalars(
            select(models.ProjectMember.user_id)
            .where(
                models.ProjectMember.project_id == project_id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(True),
            )
            .order_by(models.ProjectMember.joined_at, models.ProjectMember.id)
        )
    )


async def lead_offer_expiry(
    session: AsyncSession, project: models.Project, event: models.Event | None, now: datetime
) -> datetime:
    settings = await load_settings(session)
    if event is None or project.event_id is None:
        return now + projects.STANDALONE_TRANSFER_TTL
    formation = await session.get(models.EventFormation, event.id)
    if project.team_status == TeamStatus.FORMING.value:
        cap = formation.formation_at if formation is not None else event.starts_at
    elif project.team_status == TeamStatus.REVIEW.value:
        cap = (
            project.review_ends_at
            or (formation.review_ends_at if formation is not None else None)
            or event.starts_at
        )
    elif project.team_status == TeamStatus.LOCKED.value:
        cap = event.starts_at
    elif project.team_status == TeamStatus.ACTIVE.value:
        cap = event.ends_at
    elif project.team_status in RESULT_TEAM_STATUSES:
        return now + projects.STANDALONE_TRANSFER_TTL
    else:
        raise ProblemError(ErrorCode.INVALID_STATE, "This team cannot receive lead offers")
    return min(now + timedelta(hours=settings.lead_offer_hours), cap)


async def close_pending_lead_offers(session: AsyncSession, project_id: UUID, now: datetime) -> None:
    await session.execute(
        update(models.LeadOffer)
        .where(
            models.LeadOffer.project_id == project_id,
            models.LeadOffer.status == LeadOfferStatus.PENDING.value,
            models.LeadOffer.source == LeadOfferSource.TRANSFER.value,
        )
        .values(status=LeadOfferStatus.CANCELLED.value, responded_at=now)
    )
    await session.execute(
        update(models.LeadOffer)
        .where(
            models.LeadOffer.project_id == project_id,
            models.LeadOffer.status == LeadOfferStatus.PENDING.value,
            models.LeadOffer.source != LeadOfferSource.TRANSFER.value,
        )
        .values(status=LeadOfferStatus.CLOSED.value, responded_at=now)
    )
    return None


async def create_lead_offer(
    session: AsyncSession,
    project: models.Project,
    event: models.Event | None,
    user_id: UUID,
    now: datetime,
    *,
    source: LeadOfferSource,
    reason: str,
    offered_by: UUID | None = None,
    tier: int | None = None,
) -> models.LeadOffer:
    user = await session.get(models.User, user_id)
    if user is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    member = await session.scalar(
        select(models.ProjectMember.id)
        .where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == user_id,
            models.ProjectMember.left_at.is_(None),
        )
        .limit(1)
    )
    if member is None:
        raise ProblemError(ErrorCode.INVALID_STATE, "The offered member is not on this team")
    await close_pending_lead_offers(session, project.id, now)
    expires_at = await lead_offer_expiry(session, project, event, now)
    offer = models.LeadOffer(
        project_id=project.id,
        event_id=project.event_id,
        user_id=user_id,
        source=source.value,
        offered_by=offered_by,
        tier=tier,
        reason=reason,
        status=LeadOfferStatus.PENDING.value,
        expires_at=expires_at,
    )
    session.add(offer)
    await session.flush()
    offerer = await session.get(models.User, offered_by) if offered_by is not None else None
    if event is not None:
        await notify(
            session,
            user_id,
            LeadOfferPayload(
                link=f"/projects/{project.id}",
                event_id=event.id,
                event_title=event.title,
                project_id=project.id,
                team_name=project.name,
                lead_offer_id=offer.id,
                source=source,
                offered_member=notification_person(user),
                offered_by=notification_person(offerer) if offerer is not None else None,
                expires_at=expires_at,
            ),
            now,
            dedupe_key=f"lead_offer:{offer.id}",
        )
    else:
        await notify(
            session,
            user_id,
            projects.project_lead_offer_payload(project, offer, offerer),
            now,
            dedupe_key=f"project_lead_offer:{offer.id}",
        )
    return offer


async def lead_offer_responses(
    session: AsyncSession, offers: Sequence[models.LeadOffer], actor: models.User
) -> list[project_schemas.LeadOffer]:
    if not offers:
        return []
    projects_by_id = {
        project.id: project
        for project in await session.scalars(
            select(models.Project).where(
                models.Project.id.in_({offer.project_id for offer in offers})
            )
        )
    }
    user_ids = {offer.user_id for offer in offers} | {
        offer.offered_by for offer in offers if offer.offered_by is not None
    }
    users: dict[UUID, models.User] = {
        user.id: user
        for user in await session.scalars(select(models.User).where(models.User.id.in_(user_ids)))
    }
    views: list[project_schemas.LeadOffer] = []
    for offer in offers:
        project = projects_by_id.get(offer.project_id)
        if project is None:
            raise ProblemError(ErrorCode.NOT_FOUND)
        views.append(projects.lead_offer_view(project, offer, users=users, actor=actor))
    return views


async def lead_offer_response(
    session: AsyncSession, offer: models.LeadOffer, actor: models.User
) -> project_schemas.LeadOffer:
    return (await lead_offer_responses(session, [offer], actor))[0]


async def start_succession(
    session: AsyncSession,
    project: models.Project,
    event: models.Event | None,
    now: datetime,
    *,
    exclude: set[UUID],
    reason: str,
) -> models.LeadOffer | None:
    if event is None:
        await projects.standalone_lead_succession(session, project, now, exclude=set(exclude))
        return None
    project.lead_succession_excluded = sorted(set(exclude), key=str)
    return await _offer_next(session, project, event, now, reason=reason)


async def advance_succession(
    session: AsyncSession,
    project: models.Project,
    event: models.Event | None,
    now: datetime,
    *,
    exclude: UUID,
    reason: str,
) -> models.LeadOffer | None:
    if event is None:
        await projects.standalone_lead_succession(session, project, now, exclude={exclude})
        return None
    project.lead_succession_excluded = sorted({*project.lead_succession_excluded, exclude}, key=str)
    return await _offer_next(session, project, event, now, reason=reason)


async def _offer_next(
    session: AsyncSession,
    project: models.Project,
    event: models.Event,
    now: datetime,
    *,
    reason: str,
) -> models.LeadOffer | None:
    if project.lead_user_id is not None:
        return None
    excluded = set(project.lead_succession_excluded)
    entries = [
        entry for entry in await lead_order(session, project, now) if entry.user.id not in excluded
    ]
    if not entries:
        return await _exhausted(session, project, event, now)
    expires_at = await lead_offer_expiry(session, project, event, now)
    if expires_at <= now:
        return await _exhausted(session, project, event, now)
    return await create_lead_offer(
        session,
        project,
        event,
        entries[0].user.id,
        now,
        source=LeadOfferSource.SUCCESSION,
        reason=reason,
        tier=entries[0].tier,
    )


async def _exhausted(
    session: AsyncSession, project: models.Project, event: models.Event | None, now: datetime
) -> None:
    if project.team_status in FORCEABLE_TEAM_STATUSES and event is not None:
        await force_lead(session, project, event, now, reason="No member accepted the lead role")
    return None


async def force_lead(
    session: AsyncSession,
    project: models.Project,
    event: models.Event | None,
    now: datetime,
    *,
    reason: str,
) -> models.ProjectMember | None:
    if event is None or project.lead_user_id is not None:
        return None
    if project.team_status not in FORCEABLE_TEAM_STATUSES:
        return None
    entry = next(
        (
            entry
            for entry in await lead_order(session, project, now)
            if entry.member.state == MemberState.CONFIRMED.value
        ),
        None,
    )
    if entry is None:
        return None
    await close_pending_lead_offers(session, project.id, now)
    project.lead_user_id = entry.user.id
    project.last_lead_action_at = now
    project.lead_succession_excluded = []
    entry.member.role = MemberRole.LEAD.value
    await session.flush()
    await record_audit(
        session,
        actor=None,
        actor_kind=ActorKind.SYSTEM,
        action=AuditAction.LEAD_ASSIGN,
        entity_type=AuditEntityType.PROJECT,
        entity_id=project.id,
        before={"lead_user_id": None},
        after={"lead_user_id": str(entry.user.id), "forced": True},
        reason=reason,
    )
    await notify_many(
        session,
        await team_member_ids(session, project.id),
        LeadAssignedPayload(
            link=f"/projects/{project.id}",
            event_id=event.id,
            event_title=event.title,
            project_id=project.id,
            team_name=project.name,
            lead=notification_person(entry.user),
            reason=reason,
        ),
        now,
        dedupe_key=f"lead_assigned:{project.id}:{entry.user.id}",
    )
    await raise_alert(
        session,
        AlertKind.FORCED_LEAD,
        now,
        title=f"Lead role forced: {project.name}",
        message=f"{entry.user.display_name} was assigned as lead because nobody accepted the offer",
        link=f"/admin/projects/{project.id}",
        subject_type="project",
        subject_id=project.id,
        subject_title=project.name,
        dedupe_key=f"forced_lead:{project.id}",
    )
    return entry.member


async def locked_lead_offer(
    session: AsyncSession, offer_id: UUID, actor: models.User
) -> tuple[models.LeadOffer, models.Project, models.Event | None]:
    offer = await session.get(models.LeadOffer, offer_id)
    if offer is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    project = await session.get(models.Project, offer.project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    event = (
        await locked_formation_event(session, project.event_id, actor)
        if project.event_id is not None
        else None
    )
    project = await session.scalar(
        select(models.Project)
        .where(models.Project.id == offer.project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    offer = await session.scalar(
        select(models.LeadOffer)
        .where(models.LeadOffer.id == offer_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if offer is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return offer, project, event


async def _retire_lead_offer(
    session: AsyncSession,
    offer: models.LeadOffer,
    project: models.Project,
    event: models.Event | None,
    now: datetime,
    *,
    status: LeadOfferStatus,
    reason: str,
) -> None:
    offer.status = status.value
    offer.responded_at = now
    if offer.source != LeadOfferSource.TRANSFER.value:
        await advance_succession(session, project, event, now, exclude=offer.user_id, reason=reason)
    await session.flush()
    return None


async def expire_lead_offer(
    session: AsyncSession,
    offer: models.LeadOffer,
    project: models.Project,
    event: models.Event | None,
    now: datetime,
) -> None:
    await _retire_lead_offer(
        session,
        offer,
        project,
        event,
        now,
        status=LeadOfferStatus.EXPIRED,
        reason="The lead offer expired",
    )
    return None


async def my_lead_offers(
    session: AsyncSession, actor: models.User, status: LeadOfferStatus | None
) -> list[project_schemas.LeadOffer]:
    statement = select(models.LeadOffer).where(models.LeadOffer.user_id == actor.id)
    if status is not None:
        statement = statement.where(models.LeadOffer.status == status.value)
    statement = statement.order_by(models.LeadOffer.created_at.desc(), models.LeadOffer.id.desc())
    return await lead_offer_responses(session, list(await session.scalars(statement)), actor)


async def _persisted_problem(session: AsyncSession, detail: str) -> ProblemError:
    await session.flush()
    return ProblemError(ErrorCode.INVALID_STATE, detail)


async def accept_lead_offer(
    session: AsyncSession,
    jobs: procrastinate.App,
    offer_id: UUID,
    actor: models.User,
    now: datetime,
) -> models.LeadOffer | ProblemError:
    from hmm_core.identity.service import lock_user

    await lock_user(session, actor.id)
    offer, project, event = await locked_lead_offer(session, offer_id, actor)
    if offer.user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if projects.lead_changes_closed(project):
        raise ProblemError(ErrorCode.INVALID_STATE, "This team is closed")
    if offer.status == LeadOfferStatus.ACCEPTED.value:
        return offer
    if offer.status != LeadOfferStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This lead offer is no longer pending")
    if offer.expires_at <= now:
        await expire_lead_offer(session, offer, project, event, now)
        return await _persisted_problem(session, "This lead offer has expired")
    transfer = offer.source == LeadOfferSource.TRANSFER.value
    if project.lead_user_id is not None and not (
        transfer and project.lead_user_id == offer.offered_by
    ):
        offer.status = LeadOfferStatus.CLOSED.value
        offer.responded_at = now
        return await _persisted_problem(session, "This team already has a lead")
    member = await session.scalar(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == actor.id,
            models.ProjectMember.left_at.is_(None),
        )
    )
    if member is None:
        raise ProblemError(ErrorCode.INVALID_STATE, "You are no longer on this team")
    if (
        project.team_status is None or project.team_status in projects.POST_RESULT_TEAM_STATUSES
    ) and offer.source in (LeadOfferSource.TRANSFER.value, LeadOfferSource.SUCCESSION.value):
        settings = await load_settings(session)
        if await projects.led_projects_count(session, actor.id) >= settings.projects_led_max:
            raise ProblemError(
                ErrorCode.PROJECTS_LED_LIMIT, "You already lead the maximum number of projects"
            )
    previous_lead_id = project.lead_user_id
    if previous_lead_id is not None:
        previous = await session.scalar(
            select(models.ProjectMember).where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.user_id == previous_lead_id,
                models.ProjectMember.left_at.is_(None),
            )
        )
        if previous is not None:
            previous.role = MemberRole.MEMBER.value
            await session.flush()
    member.role = MemberRole.LEAD.value
    member.state = MemberState.CONFIRMED.value
    member.confirm_by = None
    member.confirm_kind = None
    member.confirmed_at = member.confirmed_at or now
    offer.status = LeadOfferStatus.ACCEPTED.value
    offer.responded_at = now
    project.lead_user_id = actor.id
    project.last_lead_action_at = now
    project.lead_succession_excluded = []
    await session.flush()
    if transfer:
        await record_audit(
            session,
            actor=actor.id,
            actor_kind=ActorKind.USER,
            action=AuditAction.LEAD_TRANSFER,
            entity_type=AuditEntityType.PROJECT,
            entity_id=project.id,
            before={"lead_user_id": str(offer.offered_by)},
            after={"lead_user_id": str(actor.id)},
            reason=None,
        )
    else:
        await record_audit(
            session,
            actor=actor.id,
            actor_kind=ActorKind.USER,
            action=AuditAction.LEAD_ASSIGN,
            entity_type=AuditEntityType.PROJECT,
            entity_id=project.id,
            before={"lead_user_id": None},
            after={"lead_user_id": str(actor.id), "source": offer.source},
            reason=None,
        )
    if event is not None:
        recipients = await team_member_ids(session, project.id)
        previous_user = (
            await session.get(models.User, previous_lead_id)
            if transfer and previous_lead_id is not None
            else None
        )
        if previous_user is not None:
            await notify_many(
                session,
                recipients,
                LeadTransferredPayload(
                    link=f"/projects/{project.id}",
                    event_id=event.id,
                    event_title=event.title,
                    project_id=project.id,
                    team_name=project.name,
                    previous_lead=notification_person(previous_user),
                    lead=notification_person(actor),
                ),
                now,
                dedupe_key=f"lead_transferred:{offer.id}",
            )
        else:
            await notify_many(
                session,
                recipients,
                LeadAssignedPayload(
                    link=f"/projects/{project.id}",
                    event_id=event.id,
                    event_title=event.title,
                    project_id=project.id,
                    team_name=project.name,
                    lead=notification_person(actor),
                    reason="The member accepted the lead offer",
                ),
                now,
                dedupe_key=f"lead_assigned:{project.id}:{actor.id}",
            )
    await defer_job(session, jobs, JobName.REFRESH_RECOMMENDATIONS, project_id=str(project.id))
    await session.flush()
    await session.refresh(offer)
    return offer


async def decline_lead_offer(
    session: AsyncSession, offer_id: UUID, actor: models.User, now: datetime
) -> models.LeadOffer | ProblemError:
    offer, project, event = await locked_lead_offer(session, offer_id, actor)
    if offer.user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if projects.lead_changes_closed(project):
        raise ProblemError(ErrorCode.INVALID_STATE, "This team is closed")
    if offer.status == LeadOfferStatus.DECLINED.value:
        return offer
    if offer.status != LeadOfferStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This lead offer is no longer pending")
    if offer.expires_at <= now:
        await expire_lead_offer(session, offer, project, event, now)
        return await _persisted_problem(session, "This lead offer has expired")
    offer.status = LeadOfferStatus.DECLINED.value
    offer.responded_at = now
    await session.flush()
    if offer.source == LeadOfferSource.TRANSFER.value:
        if offer.offered_by is not None and event is not None:
            await notify(
                session,
                offer.offered_by,
                LeadOfferDeclinedPayload(
                    link=f"/projects/{project.id}",
                    event_id=event.id,
                    event_title=event.title,
                    project_id=project.id,
                    team_name=project.name,
                    lead_offer_id=offer.id,
                    offered_member=notification_person(actor),
                    reason="The member declined the lead role",
                ),
                now,
                dedupe_key=f"lead_offer_declined:{offer.id}",
            )
    elif project.lead_user_id is None:
        await advance_succession(
            session,
            project,
            event,
            now,
            exclude=offer.user_id,
            reason="The next candidate in the lead order",
        )
    await session.flush()
    await session.refresh(offer)
    return offer


async def cancel_lead_offer(
    session: AsyncSession, offer_id: UUID, actor: models.User, now: datetime
) -> None:
    offer, project, _ = await locked_lead_offer(session, offer_id, actor)
    if offer.offered_by != actor.id or project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if offer.source != LeadOfferSource.TRANSFER.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "Only a transfer offer can be cancelled")
    if offer.status == LeadOfferStatus.CANCELLED.value:
        return None
    if offer.status != LeadOfferStatus.PENDING.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This lead offer is no longer pending")
    offer.status = LeadOfferStatus.CANCELLED.value
    offer.responded_at = now
    project.last_lead_action_at = now
    await session.flush()
    return None


async def expire_lead_offers(session: AsyncSession, event: models.Event, now: datetime) -> int:
    await session.execute(
        select(models.Event.id).where(models.Event.id == event.id).with_for_update()
    )
    due = (
        await session.execute(
            select(models.LeadOffer.id, models.LeadOffer.project_id)
            .where(
                models.LeadOffer.event_id == event.id,
                models.LeadOffer.status == LeadOfferStatus.PENDING.value,
                models.LeadOffer.expires_at <= now,
            )
            .order_by(models.LeadOffer.created_at, models.LeadOffer.id)
        )
    ).all()
    if not due:
        return 0
    locked = {
        project.id: project
        for project in await session.scalars(
            select(models.Project)
            .where(models.Project.id.in_({project_id for _, project_id in due}))
            .order_by(models.Project.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    expired = 0
    for offer_id, _ in due:
        offer = await session.get(
            models.LeadOffer, offer_id, with_for_update=True, populate_existing=True
        )
        if offer is None or offer.status != LeadOfferStatus.PENDING.value:
            continue
        project = locked[offer.project_id]
        if projects.lead_changes_closed(project):
            offer.status = LeadOfferStatus.EXPIRED.value
            offer.responded_at = now
        else:
            await expire_lead_offer(session, offer, project, event, now)
        expired += 1
    await session.flush()
    return expired
