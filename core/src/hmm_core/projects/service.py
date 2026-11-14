from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from hmm_contracts.common import (
    InviteStatus,
    JoinRequestStatus,
    LeadOfferSource,
    LeadOfferStatus,
    MemberRole,
    MemberState,
    ProjectStatus,
    ProjectVisibility,
    TeamStatus,
)
from pydantic import HttpUrl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.platform.schemas import ProjectRef, UserRef
from hmm_core.platform.settings import RuntimeSettings, load_settings
from hmm_core.projects import schemas

OPEN_TEAMS = frozenset({TeamStatus.FORMING, TeamStatus.REVIEW})
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
    if is_lead and other_confirmed:
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
            allowed |= {
                schemas.ProjectAction.SWAP,
                schemas.ProjectAction.MERGE,
                schemas.ProjectAction.INVITE,
            }
        if not lead_changes_closed(project):
            allowed |= {schemas.ProjectAction.TRANSFER_LEAD, schemas.ProjectAction.STEP_DOWN}
    if is_member and project.lead_user_id is None and project.status == ProjectStatus.PAUSED:
        allowed.add(schemas.ProjectAction.CLAIM_LEAD)
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
