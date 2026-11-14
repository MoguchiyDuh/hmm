from collections import defaultdict
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
    InviteStatus,
    JoinedVia,
    JoinRequestStatus,
    LeadOfferSource,
    LeadOfferStatus,
    LeftReason,
    MemberRole,
    MemberState,
    ProjectOrigin,
    ProjectStatus,
    ProjectVisibility,
    RestrictionKind,
    TeamStatus,
    Trust,
)
from hmm_contracts.notifications import (
    NotificationPerson,
    ProjectMemberLeftPayload,
    ProjectMemberRemovedPayload,
    ProjectPausedPayload,
)
from pydantic import HttpUrl, JsonValue
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.notifications.service import notify, notify_many
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.pagination import CursorParams, keyset_page
from hmm_core.platform.schemas import ProjectRef, ReasonBody, UserRef
from hmm_core.platform.settings import RuntimeSettings, load_settings
from hmm_core.projects import schemas
from hmm_core.scores.restrictions import require_unrestricted

OPEN_TEAMS = frozenset({TeamStatus.FORMING, TeamStatus.REVIEW})
POST_RESULT_TEAM_STATUSES: frozenset[str] = frozenset(
    {TeamStatus.FINISHED.value, TeamStatus.WITHDRAWN.value}
)
TERMINAL_TEAM_STATUSES: frozenset[str] = frozenset(
    {
        TeamStatus.FINISHED.value,
        TeamStatus.WITHDRAWN.value,
        TeamStatus.DISBANDED.value,
        TeamStatus.CANCELLED.value,
    }
)
STANDALONE_TRANSFER_TTL = timedelta(days=7)
LEAD_CLOSED_TEAM_STATUSES: frozenset[str] = frozenset(
    {TeamStatus.DISBANDED.value, TeamStatus.CANCELLED.value}
)
LEAD_CLOSED_PROJECT_STATUSES: frozenset[str] = frozenset(
    {ProjectStatus.ABANDONED.value, ProjectStatus.DISBANDED.value, ProjectStatus.CANCELLED.value}
)
CLOSED_PROJECT_STATUSES = LEAD_CLOSED_PROJECT_STATUSES


@dataclass(frozen=True, slots=True, kw_only=True)
class TeamSize:
    target: int
    minimum: int


@dataclass(frozen=True, slots=True, kw_only=True)
class MemberRow:
    member: models.ProjectMember
    user: models.User
    area_key: str | None


def user_ref(user: models.User) -> UserRef:
    return UserRef(id=user.id, display_name=user.display_name, avatar_url=user.avatar_url)


def project_ref(project: models.Project) -> ProjectRef:
    return ProjectRef(
        id=project.id,
        name=project.name,
        event_id=project.event_id,
        team_status=TeamStatus(project.team_status) if project.team_status else None,
    )


def team_size(event: models.Event | None, settings: RuntimeSettings) -> TeamSize:
    if event is None:
        return TeamSize(
            target=settings.team_size_default_max, minimum=settings.team_size_default_min
        )
    return TeamSize(
        target=event.team_size_max or settings.team_size_default_max,
        minimum=event.team_size_min or settings.team_size_default_min,
    )


def project_listing(project: models.Project, area_keys: list[str]) -> schemas.ProjectListing:
    return schemas.ProjectListing(
        view="limited",
        id=project.id,
        name=project.name,
        area_keys=area_keys,
        allowed_actions=[],
    )


def lead_changes_closed(project: models.Project) -> bool:
    return (
        project.team_status in LEAD_CLOSED_TEAM_STATUSES
        or project.status in LEAD_CLOSED_PROJECT_STATUSES
    )


def member_actions(
    project: models.Project, member: models.ProjectMember, actor: models.User
) -> list[schemas.ProjectMemberAction]:
    is_lead = actor.id == project.lead_user_id
    other_confirmed = member.user_id != actor.id and member.state == MemberState.CONFIRMED
    allowed: set[schemas.ProjectMemberAction] = set()
    if actor.id == member.user_id and member.state == MemberState.PROPOSED:
        allowed |= {
            schemas.ProjectMemberAction.CONFIRM_PLACEMENT,
            schemas.ProjectMemberAction.DECLINE_PLACEMENT,
        }
    if (
        is_lead
        and other_confirmed
        and (not member.is_team_row or project.team_status == TeamStatus.REVIEW)
    ):
        allowed.add(schemas.ProjectMemberAction.REMOVE)
    if actor.is_admin:
        allowed.add(schemas.ProjectMemberAction.MOVE)
    if is_lead and other_confirmed and not project_closed(project):
        allowed.add(schemas.ProjectMemberAction.TRANSFER_LEAD)
    if is_lead or actor.is_admin:
        allowed.add(schemas.ProjectMemberAction.EDIT)
    return [action for action in schemas.ProjectMemberAction if action in allowed]


def project_member(
    project: models.Project, row: MemberRow, actor: models.User
) -> schemas.ProjectMember:
    member = row.member
    return schemas.ProjectMember.model_validate(
        {
            "id": member.id,
            "project_id": member.project_id,
            "event_id": member.event_id,
            "user_id": member.user_id,
            "user": user_ref(row.user),
            "role": member.role,
            "area_key": row.area_key,
            "state": member.state,
            "trust": member.trust,
            "joined_via": member.joined_via,
            "proposal_reason": member.proposal_reason,
            "confirm_by": member.confirm_by,
            "confirm_kind": member.confirm_kind,
            "is_team_row": member.is_team_row,
            "joined_at": member.joined_at,
            "confirmed_at": member.confirmed_at,
            "left_at": member.left_at,
            "left_reason": member.left_reason,
            "allowed_actions": member_actions(project, member, actor),
        }
    )


def project_actions(
    project: models.Project,
    *,
    members: Sequence[MemberRow],
    minimum: int,
    actor: models.User,
) -> list[schemas.ProjectAction]:
    is_lead = actor.id == project.lead_user_id
    is_member = any(row.member.user_id == actor.id for row in members)
    allowed: set[schemas.ProjectAction] = set()
    if is_lead or actor.is_admin:
        allowed.add(schemas.ProjectAction.EDIT)
    if is_member:
        allowed.add(schemas.ProjectAction.LEAVE)
    if is_lead:
        if project.team_status == TeamStatus.FORMING:
            allowed |= {
                schemas.ProjectAction.ADD_MEMBER,
                schemas.ProjectAction.READY
                if project.ready_at is None
                else schemas.ProjectAction.UNREADY,
                schemas.ProjectAction.UNCLAIM,
            }
        if project.team_status == TeamStatus.LOCKED:
            if project.seats_closed_at is None and len(members) >= minimum:
                allowed.add(schemas.ProjectAction.CLOSE_SEATS)
            if project.seats_closed_at is not None:
                allowed.add(schemas.ProjectAction.REOPEN_SEATS)
        if project.team_status in OPEN_TEAMS:
            allowed.add(schemas.ProjectAction.INVITE)
        if project.team_status == TeamStatus.REVIEW:
            allowed |= {schemas.ProjectAction.SWAP, schemas.ProjectAction.MERGE}
        if not lead_changes_closed(project):
            allowed |= {schemas.ProjectAction.TRANSFER_LEAD, schemas.ProjectAction.STEP_DOWN}
    if is_member and project.lead_user_id is None and project.status == ProjectStatus.PAUSED:
        allowed.add(schemas.ProjectAction.CLAIM_LEAD)
    if is_lead and lifecycle_open(project):
        if project.status in (ProjectStatus.IDEA.value, ProjectStatus.PAUSED.value):
            allowed.add(schemas.ProjectAction.START)
        if project.status in (ProjectStatus.IDEA.value, ProjectStatus.ACTIVE.value):
            allowed.add(schemas.ProjectAction.PAUSE)
        if project.status in (
            ProjectStatus.IDEA.value,
            ProjectStatus.ACTIVE.value,
            ProjectStatus.PAUSED.value,
        ):
            allowed.add(schemas.ProjectAction.ABANDON)
        if project.status == ProjectStatus.SHIPPED.value:
            allowed.add(schemas.ProjectAction.CONTINUE)
        if project.team_status is None:
            allowed.add(schemas.ProjectAction.INVITE)
    if (
        not is_member
        and project.team_status is None
        and project.recruiting is not None
        and project.lead_user_id is not None
        and project.status
        in (
            ProjectStatus.IDEA.value,
            ProjectStatus.ACTIVE.value,
        )
    ):
        allowed.add(schemas.ProjectAction.REQUEST_JOIN)
    if actor.is_admin:
        allowed |= {
            schemas.ProjectAction.DISBAND,
            schemas.ProjectAction.SET_STATUS,
            schemas.ProjectAction.SET_LEAD,
        }
    return [action for action in schemas.ProjectAction if action in allowed]


def open_seats(project: models.Project, *, members: int, target: int) -> int | None:
    if project.team_status != TeamStatus.LOCKED:
        return None
    return max(0, target - members)


def invite_view(
    project: models.Project,
    invite: models.Invite,
    *,
    users: dict[UUID, models.User],
    area_keys: dict[UUID, str],
    actor: models.User,
) -> schemas.Invite:
    inviter = users.get(invite.invited_by) if invite.invited_by is not None else None
    allowed: set[schemas.InviteAction] = set()
    if invite.status == InviteStatus.PENDING:
        if actor.id == project.lead_user_id:
            allowed.add(schemas.InviteAction.WITHDRAW)
        if actor.id == invite.user_id:
            allowed |= {schemas.InviteAction.ACCEPT, schemas.InviteAction.DECLINE}
    return schemas.Invite.model_validate(
        {
            "id": invite.id,
            "project_id": invite.project_id,
            "project": project_ref(project),
            "event_id": invite.event_id,
            "user_id": invite.user_id,
            "user": user_ref(users[invite.user_id]),
            "invited_by": invite.invited_by,
            "inviter": user_ref(inviter) if inviter is not None else None,
            "kind": invite.kind,
            "message": invite.message,
            "area_key": area_keys.get(invite.area_id) if invite.area_id is not None else None,
            "status": invite.status,
            "closed_reason": invite.closed_reason,
            "expires_at": invite.expires_at,
            "responded_at": invite.responded_at,
            "created_at": invite.created_at,
            "updated_at": invite.updated_at,
            "allowed_actions": [action for action in schemas.InviteAction if action in allowed],
        }
    )


def join_request_view(
    project: models.Project,
    request: models.JoinRequest,
    *,
    users: dict[UUID, models.User],
    actor: models.User,
) -> schemas.JoinRequest:
    allowed: set[schemas.JoinRequestAction] = set()
    if request.status == JoinRequestStatus.PENDING:
        if actor.id == project.lead_user_id:
            allowed |= {schemas.JoinRequestAction.ACCEPT, schemas.JoinRequestAction.REJECT}
        if actor.id == request.user_id:
            allowed.add(schemas.JoinRequestAction.WITHDRAW)
    return schemas.JoinRequest.model_validate(
        {
            "id": request.id,
            "project_id": request.project_id,
            "project": project_ref(project),
            "event_id": request.event_id,
            "user_id": request.user_id,
            "user": user_ref(users[request.user_id]),
            "note": request.note or "",
            "status": request.status,
            "responded_at": request.responded_at,
            "created_at": request.created_at,
            "updated_at": request.updated_at,
            "allowed_actions": [
                action for action in schemas.JoinRequestAction if action in allowed
            ],
        }
    )


def lead_offer_view(
    project: models.Project,
    offer: models.LeadOffer,
    *,
    users: dict[UUID, models.User],
    actor: models.User,
) -> schemas.LeadOffer:
    offerer = users.get(offer.offered_by) if offer.offered_by is not None else None
    pending = offer.status == LeadOfferStatus.PENDING.value
    allowed: set[schemas.LeadOfferAction] = set()
    if pending and actor.id == offer.user_id:
        allowed |= {schemas.LeadOfferAction.ACCEPT, schemas.LeadOfferAction.DECLINE}
    if (
        pending
        and offer.source == LeadOfferSource.TRANSFER.value
        and offer.offered_by == actor.id
        and project.lead_user_id == actor.id
    ):
        allowed.add(schemas.LeadOfferAction.CANCEL)
    return schemas.LeadOffer.model_validate(
        {
            "id": offer.id,
            "project_id": offer.project_id,
            "project": project_ref(project),
            "event_id": offer.event_id,
            "user_id": offer.user_id,
            "user": user_ref(users[offer.user_id]),
            "source": offer.source,
            "offered_by": offer.offered_by,
            "offerer": user_ref(offerer) if offerer is not None else None,
            "tier": offer.tier,
            "reason": offer.reason,
            "status": offer.status,
            "expires_at": offer.expires_at,
            "responded_at": offer.responded_at,
            "created_at": offer.created_at,
            "updated_at": offer.updated_at,
            "allowed_actions": [action for action in schemas.LeadOfferAction if action in allowed],
        }
    )


def project_detail(
    project: models.Project,
    *,
    area_keys: list[str],
    members: Sequence[MemberRow],
    team_size: TeamSize,
    formation: models.EventFormation | None,
    actor: models.User,
    include_pending: bool,
    invites: list[schemas.Invite],
    join_requests: list[schemas.JoinRequest],
    lead_offer: schemas.LeadOffer | None,
) -> schemas.Project:
    target = (
        formation.target_override
        if formation is not None and formation.target_override is not None
        else team_size.target
    )
    return schemas.Project.model_validate(
        {
            "view": "full",
            "id": project.id,
            "name": project.name,
            "event_id": project.event_id,
            "team_status": project.team_status,
            "area_keys": area_keys,
            "status": project.status,
            "visibility": project.visibility,
            "lead_user_id": project.lead_user_id,
            "recruiting": schemas.Recruiting.model_validate(project.recruiting)
            if project.recruiting
            else None,
            "origin": project.origin,
            "created_at": project.created_at,
            "updated_at": project.updated_at,
            "description_md": project.description_md,
            "goal": project.goal,
            "links": [HttpUrl(str(link)) for link in project.links]
            if isinstance(project.links, list)
            else [],
            "ready_at": project.ready_at,
            "review_ends_at": project.review_ends_at,
            "formed_in_run_id": project.formed_in_run_id,
            "locked_at": project.locked_at,
            "seat_open_since": project.seat_open_since,
            "seats_closed_at": project.seats_closed_at,
            "open_seats": open_seats(project, members=len(members), target=target),
            "last_lead_action_at": project.last_lead_action_at,
            "last_progress_at": project.last_progress_at,
            "members": [project_member(project, row, actor) for row in members],
            "pending_invites": invites if include_pending else None,
            "pending_join_requests": join_requests if include_pending else None,
            "pending_lead_offer": lead_offer if include_pending else None,
            "allowed_actions": project_actions(
                project, members=members, minimum=team_size.minimum, actor=actor
            ),
        }
    )


async def standalone_lead_succession(
    session: AsyncSession, project: models.Project, now: datetime, *, exclude: set[UUID]
) -> None:
    return None


async def project_views(
    session: AsyncSession,
    projects: Sequence[models.Project],
    actor: models.User,
    now: datetime,
) -> list[schemas.ProjectView]:
    if not projects:
        return []
    ids = [project.id for project in projects]
    area_keys: defaultdict[UUID, list[str]] = defaultdict(list)
    for project_id, key in await session.execute(
        select(models.ProjectArea.project_id, models.Area.key)
        .join(models.Area, models.Area.id == models.ProjectArea.area_id)
        .where(models.ProjectArea.project_id.in_(ids))
        .order_by(models.Area.sort, models.Area.key)
    ):
        area_keys[project_id].append(key)
    members: defaultdict[UUID, list[MemberRow]] = defaultdict(list)
    for member, user, key in await session.execute(
        select(models.ProjectMember, models.User, models.Area.key)
        .join(models.User, models.User.id == models.ProjectMember.user_id)
        .outerjoin(models.Area, models.Area.id == models.ProjectMember.area_id)
        .where(models.ProjectMember.project_id.in_(ids), models.ProjectMember.left_at.is_(None))
        .order_by(
            models.ProjectMember.role != MemberRole.LEAD,
            models.ProjectMember.joined_at,
            models.ProjectMember.id,
        )
    ):
        members[member.project_id].append(MemberRow(member=member, user=user, area_key=key))
    full = {
        project.id
        for project in projects
        if project.visibility == ProjectVisibility.PUBLIC
        or actor.is_admin
        or actor.id == project.lead_user_id
        or any(row.member.user_id == actor.id for row in members[project.id])
    }
    pending = {
        project.id
        for project in projects
        if project.id in full and (actor.is_admin or actor.id == project.lead_user_id)
    }
    invites: defaultdict[UUID, list[models.Invite]] = defaultdict(list)
    join_requests: defaultdict[UUID, list[models.JoinRequest]] = defaultdict(list)
    lead_offers: dict[UUID, models.LeadOffer] = {}
    users: dict[UUID, models.User] = {}
    invite_areas: dict[UUID, str] = {}
    if pending:
        for invite in await session.scalars(
            select(models.Invite)
            .where(
                models.Invite.project_id.in_(pending),
                models.Invite.status == InviteStatus.PENDING,
            )
            .order_by(models.Invite.created_at, models.Invite.id)
        ):
            invites[invite.project_id].append(invite)
        for request in await session.scalars(
            select(models.JoinRequest)
            .where(
                models.JoinRequest.project_id.in_(pending),
                models.JoinRequest.status == JoinRequestStatus.PENDING,
            )
            .order_by(models.JoinRequest.created_at, models.JoinRequest.id)
        ):
            join_requests[request.project_id].append(request)
        for offer in await session.scalars(
            select(models.LeadOffer).where(
                models.LeadOffer.project_id.in_(pending),
                models.LeadOffer.status == LeadOfferStatus.PENDING,
            )
        ):
            lead_offers[offer.project_id] = offer
        user_ids = (
            {invite.user_id for rows in invites.values() for invite in rows}
            | {invite.invited_by for rows in invites.values() for invite in rows}
            | {request.user_id for rows in join_requests.values() for request in rows}
            | {offer.user_id for offer in lead_offers.values()}
            | {offer.offered_by for offer in lead_offers.values()}
        ) - {None}
        if user_ids:
            users = {
                user.id: user
                for user in await session.scalars(
                    select(models.User).where(models.User.id.in_(user_ids))
                )
            }
        area_ids = {
            invite.area_id
            for rows in invites.values()
            for invite in rows
            if invite.area_id is not None
        }
        if area_ids:
            invite_areas = {
                area.id: area.key
                for area in await session.scalars(
                    select(models.Area).where(models.Area.id.in_(area_ids))
                )
            }
    event_ids = {
        project.event_id
        for project in projects
        if project.id in full and project.team_status is not None and project.event_id is not None
    }
    events: dict[UUID, models.Event] = {}
    formations: dict[UUID, models.EventFormation] = {}
    if event_ids:
        events = {
            event.id: event
            for event in await session.scalars(
                select(models.Event).where(models.Event.id.in_(event_ids))
            )
        }
        formations = {
            row.event_id: row
            for row in await session.scalars(
                select(models.EventFormation).where(models.EventFormation.event_id.in_(event_ids))
            )
        }
    settings = await load_settings(session) if full else None
    views: list[schemas.ProjectView] = []
    for project in projects:
        keys = area_keys[project.id]
        if project.id not in full or settings is None:
            views.append(project_listing(project, keys))
            continue
        is_team = project.team_status is not None and project.event_id is not None
        event = events.get(project.event_id) if is_team and project.event_id else None
        formation = formations.get(project.event_id) if is_team and project.event_id else None
        offer = lead_offers.get(project.id)
        views.append(
            project_detail(
                project,
                area_keys=keys,
                members=members[project.id],
                team_size=team_size(event, settings),
                formation=formation,
                actor=actor,
                include_pending=project.id in pending,
                invites=[
                    invite_view(project, invite, users=users, area_keys=invite_areas, actor=actor)
                    for invite in invites[project.id]
                ],
                join_requests=[
                    join_request_view(project, request, users=users, actor=actor)
                    for request in join_requests[project.id]
                ],
                lead_offer=lead_offer_view(project, offer, users=users, actor=actor)
                if offer is not None
                else None,
            )
        )
    return views


async def remove_project_member(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    user_id: UUID,
    body: ReasonBody | None,
    actor: models.User,
    now: datetime,
) -> None:
    from hmm_core.formation import service as formation
    from hmm_core.recommendations import service as recommendations

    project = await session.get(models.Project, project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if project.team_status is not None:
        await formation.review_remove_member(session, jobs, project_id, user_id, actor, now)
        return
    project = await _locked_project(session, project_id)
    if project.status in CLOSED_PROJECT_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE, "This project is closed")
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if user_id == actor.id:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "You cannot remove yourself; leave the project instead"
        )
    member = await session.scalar(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == user_id,
            models.ProjectMember.left_at.is_(None),
        )
    )
    if member is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    previous: dict[str, JsonValue] = {"role": member.role, "state": member.state}
    member.left_at = now
    member.left_reason = LeftReason.REMOVED.value
    project.last_lead_action_at = now
    await session.flush()
    await session.refresh(project)
    removed = await session.get(models.User, user_id)
    if removed is not None:
        await notify(
            session,
            removed.id,
            ProjectMemberRemovedPayload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                member=NotificationPerson(user_id=removed.id, display_name=removed.display_name),
                actor=NotificationPerson(user_id=actor.id, display_name=actor.display_name),
                reason=body.reason if body is not None else None,
            ),
            now,
            dedupe_key=f"project_member_removed:{member.id}",
        )
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.USER,
        action=AuditAction.MEMBER_REMOVE,
        entity_type=AuditEntityType.PROJECT_MEMBER,
        entity_id=member.id,
        before=previous,
        after={"left_reason": LeftReason.REMOVED.value},
        reason=body.reason if body is not None else None,
    )
    await recommendations.enqueue_project_refresh(session, jobs, project.id)
    await session.flush()


async def leave_project(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    actor: models.User,
    now: datetime,
) -> None:
    from hmm_core.formation import service as formation
    from hmm_core.recommendations import service as recommendations

    project = await session.get(models.Project, project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if project.team_status is not None:
        await formation.leave_team(session, jobs, project_id, actor, now)
        return
    project = await _locked_project(session, project_id)
    member = await session.scalar(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == actor.id,
            models.ProjectMember.left_at.is_(None),
        )
    )
    if member is None:
        raise ProblemError(ErrorCode.FORBIDDEN)
    was_lead = project.lead_user_id == actor.id
    member.left_at = now
    member.left_reason = LeftReason.LEFT.value
    if was_lead:
        project.lead_user_id = None
        project.last_lead_action_at = now
        await record_audit(
            session,
            actor=actor.id,
            actor_kind=ActorKind.USER,
            action=AuditAction.LEAD_STEP_DOWN,
            entity_type=AuditEntityType.PROJECT,
            entity_id=project.id,
            before={"lead_user_id": str(actor.id)},
            after={"lead_user_id": None},
            reason=None,
        )
    await session.flush()
    await session.refresh(project)
    remaining = await project_member_ids(session, project.id)
    await notify_many(
        session,
        remaining,
        ProjectMemberLeftPayload(
            link=f"/projects/{project.id}",
            project_id=project.id,
            project_name=project.name,
            member=NotificationPerson(user_id=actor.id, display_name=actor.display_name),
            member_count=len(remaining),
        ),
        now,
        dedupe_key=f"project_member_left:{member.id}",
    )
    if was_lead:
        await formation.start_succession(
            session,
            project,
            None,
            now,
            exclude={actor.id},
            reason="The lead left the project",
        )
    await recommendations.enqueue_project_refresh(session, jobs, project.id)
    await session.flush()


async def claim_project_lead(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    actor: models.User,
    now: datetime,
) -> schemas.Project:
    from hmm_core.formation import service as formation
    from hmm_core.identity.service import lock_user
    from hmm_core.recommendations import service as recommendations

    project = await session.get(models.Project, project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    if project.team_status is not None and project.team_status not in POST_RESULT_TEAM_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE, "Claim the lead through the hackathon flow")
    await lock_user(session, actor.id)
    project = await _locked_project(session, project_id)
    if project.team_status is not None and project.team_status not in POST_RESULT_TEAM_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE, "Claim the lead through the hackathon flow")
    member = await session.scalar(
        select(models.ProjectMember).where(
            models.ProjectMember.project_id == project.id,
            models.ProjectMember.user_id == actor.id,
            models.ProjectMember.left_at.is_(None),
        )
    )
    if member is None:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if project.lead_user_id is not None:
        raise ProblemError(ErrorCode.INVALID_STATE, "This project already has a lead")
    if project.status != ProjectStatus.PAUSED.value:
        raise ProblemError(ErrorCode.INVALID_STATE, "This project is not paused")
    if (
        await led_projects_count(session, actor.id)
        >= (await load_settings(session)).projects_led_max
    ):
        raise ProblemError(
            ErrorCode.PROJECTS_LED_LIMIT, "You already lead the maximum number of projects"
        )
    project.lead_user_id = actor.id
    project.last_lead_action_at = now
    project.lead_succession_excluded = []
    member.role = MemberRole.LEAD.value
    member.state = MemberState.CONFIRMED.value
    member.confirmed_at = member.confirmed_at or now
    member.confirm_by = None
    member.confirm_kind = None
    await formation.close_pending_lead_offers(session, project.id, now)
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.USER,
        action=AuditAction.LEAD_ASSIGN,
        entity_type=AuditEntityType.PROJECT,
        entity_id=project.id,
        before={"lead_user_id": None},
        after={"lead_user_id": str(actor.id), "source": "claim"},
        reason=None,
    )
    await recommendations.enqueue_project_refresh(session, jobs, project.id)
    await session.flush()
    await session.refresh(project)
    return await project_response(session, project.id, actor, now)


def project_closed(project: models.Project) -> bool:
    return (
        project.team_status in TERMINAL_TEAM_STATUSES or project.status in CLOSED_PROJECT_STATUSES
    )


def lifecycle_open(project: models.Project) -> bool:
    return (
        project.team_status is None or project.team_status in POST_RESULT_TEAM_STATUSES
    ) and project.status not in CLOSED_PROJECT_STATUSES


async def _locked_project(session: AsyncSession, project_id: UUID) -> models.Project:
    project = await session.scalar(
        select(models.Project)
        .where(models.Project.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return project


async def _transition(
    session: AsyncSession,
    project_id: UUID,
    actor: models.User,
    now: datetime,
    *,
    target: ProjectStatus,
    allowed_from: Sequence[ProjectStatus],
    message: str,
    notify_members: bool,
) -> schemas.Project:
    project = await _locked_project(session, project_id)
    if not lifecycle_open(project):
        raise ProblemError(
            ErrorCode.INVALID_STATE, "This project's lifecycle follows its hackathon team"
        )
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    if project.status not in {status.value for status in allowed_from}:
        raise ProblemError(ErrorCode.INVALID_STATE, message)
    project.status = target.value
    project.last_lead_action_at = now
    await session.flush()
    await session.refresh(project)
    if notify_members:
        members = [uid for uid in await project_member_ids(session, project.id) if uid != actor.id]
        await notify_many(
            session,
            members,
            ProjectPausedPayload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                status=ProjectStatus.PAUSED,
                reason="The lead paused the project",
            ),
            now,
            dedupe_key=None,
        )
    await _sync_recruiting(session, project, actor.id, now)
    await session.flush()
    return await project_response(session, project.id, actor, now)


async def start_project(
    session: AsyncSession, project_id: UUID, actor: models.User, now: datetime
) -> schemas.Project:
    return await _transition(
        session,
        project_id,
        actor,
        now,
        target=ProjectStatus.ACTIVE,
        allowed_from=(ProjectStatus.IDEA, ProjectStatus.PAUSED),
        message="This project cannot be started",
        notify_members=False,
    )


async def pause_project(
    session: AsyncSession, project_id: UUID, actor: models.User, now: datetime
) -> schemas.Project:
    return await _transition(
        session,
        project_id,
        actor,
        now,
        target=ProjectStatus.PAUSED,
        allowed_from=(ProjectStatus.IDEA, ProjectStatus.ACTIVE),
        message="This project cannot be paused",
        notify_members=True,
    )


async def abandon_project(
    session: AsyncSession, project_id: UUID, actor: models.User, now: datetime
) -> schemas.Project:
    return await _transition(
        session,
        project_id,
        actor,
        now,
        target=ProjectStatus.ABANDONED,
        allowed_from=(ProjectStatus.IDEA, ProjectStatus.ACTIVE, ProjectStatus.PAUSED),
        message="This project cannot be abandoned",
        notify_members=False,
    )


async def continue_project(
    session: AsyncSession, project_id: UUID, actor: models.User, now: datetime
) -> schemas.Project:
    return await _transition(
        session,
        project_id,
        actor,
        now,
        target=ProjectStatus.ACTIVE,
        allowed_from=(ProjectStatus.SHIPPED,),
        message="This project cannot be continued",
        notify_members=False,
    )


async def update_project(
    session: AsyncSession,
    jobs: procrastinate.App,
    project_id: UUID,
    body: schemas.UpdateProjectBody,
    actor: models.User,
    now: datetime,
) -> schemas.Project:
    from hmm_core.feed import service as feed
    from hmm_core.recommendations import service as recommendations

    project = await _locked_project(session, project_id)
    if project.status in CLOSED_PROJECT_STATUSES:
        raise ProblemError(ErrorCode.INVALID_STATE, "This project is closed")
    if project.team_status in (TeamStatus.DISBANDED.value, TeamStatus.CANCELLED.value):
        raise ProblemError(ErrorCode.INVALID_STATE, "This team is closed")
    if project.lead_user_id != actor.id:
        raise ProblemError(ErrorCode.FORBIDDEN)
    changed = False
    became_members = False
    for field in ("name", "description_md", "goal"):
        value = getattr(body, field)
        if value is not None and value != getattr(project, field):
            setattr(project, field, value)
            changed = True
    if body.links is not None:
        links: list[JsonValue] = [str(link) for link in body.links]
        if links != project.links:
            project.links = links
            changed = True
    if body.visibility is not None and body.visibility.value != project.visibility:
        project.visibility = body.visibility.value
        became_members = body.visibility == ProjectVisibility.MEMBERS
        changed = True
    if body.area_keys is not None:
        area_ids = await resolve_project_areas(session, body.area_keys)
        current_ids = list(
            await session.scalars(
                select(models.ProjectArea.area_id).where(
                    models.ProjectArea.project_id == project.id
                )
            )
        )
        if set(area_ids) != set(current_ids):
            await session.execute(
                delete(models.ProjectArea).where(models.ProjectArea.project_id == project.id)
            )
            session.add_all(
                [models.ProjectArea(project_id=project.id, area_id=area_id) for area_id in area_ids]
            )
            changed = True
    if "recruiting" in body.model_fields_set:
        if project.team_status is not None:
            raise ProblemError(ErrorCode.INVALID_STATE, "Hackathon teams recruit through formation")
        new_recruiting: JsonValue = None
        if body.recruiting is not None:
            await resolve_project_areas(session, body.recruiting.areas)
            new_recruiting = {
                "areas": [str(key) for key in body.recruiting.areas],
                "note": body.recruiting.note,
            }
        if new_recruiting != project.recruiting:
            project.recruiting = new_recruiting
            changed = True
    if not changed:
        return await project_response(session, project.id, actor, now)
    project.last_lead_action_at = now
    await session.flush()
    await session.refresh(project)
    if became_members:
        await feed.hide_feed_subject(session, "project", project.id, now)
    await _sync_recruiting(session, project, actor.id, now)
    await recommendations.enqueue_project_refresh(session, jobs, project.id)
    await session.flush()
    return await project_response(session, project.id, actor, now)


async def resolve_project_areas(session: AsyncSession, keys: Sequence[str]) -> list[UUID]:
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            raise ProblemError(ErrorCode.VALIDATION_ERROR, f"Duplicate area: {key}")
        seen.add(key)
    if not keys:
        return []
    found: dict[str, UUID] = {
        key: area_id
        for area_id, key in await session.execute(
            select(models.Area.id, models.Area.key).where(
                models.Area.key.in_(list(dict.fromkeys(keys))), models.Area.archived_at.is_(None)
            )
        )
    }
    for key in keys:
        if key not in found:
            raise ProblemError(ErrorCode.VALIDATION_ERROR, f"Unknown area: {key}")
    return [found[key] for key in keys]


async def led_projects_count(session: AsyncSession, user_id: UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(models.Project)
            .where(
                models.Project.lead_user_id == user_id,
                models.Project.status.notin_(sorted(CLOSED_PROJECT_STATUSES)),
            )
        )
        or 0
    )


async def project_member_ids(session: AsyncSession, project_id: UUID) -> list[UUID]:
    return list(
        await session.scalars(
            select(models.ProjectMember.user_id)
            .where(
                models.ProjectMember.project_id == project_id,
                models.ProjectMember.left_at.is_(None),
            )
            .order_by(models.ProjectMember.joined_at, models.ProjectMember.id)
        )
    )


async def _sync_recruiting(
    session: AsyncSession, project: models.Project, actor_id: UUID, now: datetime
) -> None:
    from hmm_core.feed import service as feed

    await feed.record_project_recruiting(session, project, actor_id, now)


async def project_response(
    session: AsyncSession, project_id: UUID, actor: models.User, now: datetime
) -> schemas.Project:
    project = await session.get(models.Project, project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    view = (await project_views(session, [project], actor, now))[0]
    if not isinstance(view, schemas.Project):
        raise ProblemError(ErrorCode.NOT_FOUND)
    return view


async def get_project(
    session: AsyncSession, project_id: UUID, actor: models.User, now: datetime
) -> schemas.ProjectView:
    project = await session.get(models.Project, project_id)
    if project is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return (await project_views(session, [project], actor, now))[0]


async def list_projects(
    session: AsyncSession,
    actor: models.User,
    pagination: CursorParams,
    *,
    status: ProjectStatus | None,
    area: str | None,
    recruiting: bool | None,
    event_id: UUID | None,
    member_id: UUID | None,
    now: datetime,
) -> schemas.ProjectListViewPage:
    statement = select(models.Project)
    if status is not None:
        statement = statement.where(models.Project.status == status.value)
    if area is not None:
        statement = statement.where(
            models.Project.id.in_(
                select(models.ProjectArea.project_id)
                .join(models.Area, models.Area.id == models.ProjectArea.area_id)
                .where(models.Area.key == area)
            )
        )
    if recruiting is True:
        statement = statement.where(models.Project.recruiting.is_not(None))
    elif recruiting is False:
        statement = statement.where(models.Project.recruiting.is_(None))
    if event_id is not None:
        statement = statement.where(models.Project.event_id == event_id)
    if member_id is not None:
        statement = statement.where(
            models.Project.id.in_(
                select(models.ProjectMember.project_id).where(
                    models.ProjectMember.user_id == member_id,
                    models.ProjectMember.left_at.is_(None),
                )
            )
        )
    page = await keyset_page(
        session,
        statement,
        pagination,
        sort=[models.Project.created_at],
        id_column=models.Project.id,
        serialize=lambda row: row,
        descending=True,
    )
    return schemas.ProjectListViewPage(
        items=await project_views(session, page.items, actor, now),
        next_cursor=page.next_cursor,
    )


async def list_my_projects(
    session: AsyncSession,
    actor: models.User,
    status: ProjectStatus | TeamStatus | None,
    now: datetime,
) -> list[schemas.ProjectSummary]:
    statement = (
        select(models.Project)
        .join(models.ProjectMember, models.ProjectMember.project_id == models.Project.id)
        .where(models.ProjectMember.user_id == actor.id, models.ProjectMember.left_at.is_(None))
    )
    if status is not None:
        statement = statement.where(
            or_(models.Project.status == status.value, models.Project.team_status == status.value)
        )
    rows = list(
        await session.scalars(
            statement.order_by(models.Project.updated_at.desc(), models.Project.id.desc())
        )
    )
    views = await project_views(session, rows, actor, now)
    return [
        schemas.ProjectSummary.model_validate(view)
        for view in views
        if isinstance(view, schemas.Project)
    ]


async def create_project(
    session: AsyncSession,
    jobs: procrastinate.App,
    body: schemas.CreateProjectBody,
    actor: models.User,
    now: datetime,
) -> schemas.Project:
    from hmm_core.identity.service import lock_user
    from hmm_core.recommendations import service as recommendations

    await require_unrestricted(session, actor.id, RestrictionKind.CREATING_PROJECTS, now)
    if body.event_id is not None and await session.get(models.Event, body.event_id) is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    area_ids = await resolve_project_areas(session, body.area_keys)
    await lock_user(session, actor.id)
    if (
        await led_projects_count(session, actor.id)
        >= (await load_settings(session)).projects_led_max
    ):
        raise ProblemError(
            ErrorCode.PROJECTS_LED_LIMIT, "You already lead the maximum number of projects"
        )
    project = models.Project(
        event_id=body.event_id,
        name=body.name,
        description_md=body.description_md,
        goal=body.goal,
        links=[str(link) for link in body.links],
        visibility=body.visibility.value,
        recruiting=None,
        lead_user_id=actor.id,
        status=ProjectStatus.IDEA.value,
        team_status=None,
        origin=ProjectOrigin.STANDALONE.value,
        ready_at=None,
        review_ends_at=None,
        formed_in_run_id=None,
        locked_at=None,
        seat_open_since=None,
        seats_closed_at=None,
        last_lead_action_at=now,
        last_progress_at=now,
    )
    session.add(project)
    await session.flush()
    session.add_all(
        [models.ProjectArea(project_id=project.id, area_id=area_id) for area_id in area_ids]
    )
    session.add(
        models.ProjectMember(
            project_id=project.id,
            event_id=None,
            user_id=actor.id,
            role=MemberRole.LEAD.value,
            area_id=None,
            state=MemberState.CONFIRMED.value,
            trust=Trust.SELF.value,
            joined_via=JoinedVia.CREATE.value,
            proposal_reason=None,
            confirm_by=None,
            confirm_kind=None,
            is_team_row=False,
            joined_at=now,
            confirmed_at=now,
        )
    )
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.USER,
        action=AuditAction.PROJECT_CREATE,
        entity_type=AuditEntityType.PROJECT,
        entity_id=project.id,
        before=None,
        after={
            "name": project.name,
            "origin": project.origin,
            "visibility": project.visibility,
            "lead_user_id": str(actor.id),
            "event_id": str(project.event_id) if project.event_id is not None else None,
        },
        reason=None,
    )
    await recommendations.enqueue_project_refresh(session, jobs, project.id)
    await session.flush()
    return await project_response(session, project.id, actor, now)
