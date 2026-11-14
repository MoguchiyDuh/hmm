from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from hmm_contracts.algo import Fact, MemberHistory
from hmm_contracts.common import (
    ConfirmationFactType,
    ErrorCode,
    EventStatus,
    FactType,
    FlagStatus,
    MemberState,
    MentorshipStatus,
    ProjectVisibility,
)
from pydantic import HttpUrl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.feed import schemas
from hmm_core.feed.confirmations import confirmation_candidates, withdraw_allowed
from hmm_core.identity.service import ONBOARDED
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.pagination import CursorParams, decode_cursor, encode_cursor
from hmm_core.platform.schemas import AreaRef, EventRef, ProjectRef
from hmm_core.platform.settings import load_settings
from hmm_core.scores.history import load_member_history

type FactKey = tuple[FactType, UUID]

VISIBLE_EVENT_STATUSES = frozenset({EventStatus.PUBLISHED, EventStatus.ARCHIVED})


@dataclass(frozen=True, slots=True)
class _Lookups:
    area_names: dict[str, str]
    event_statuses: dict[UUID, str]
    project_visible: dict[UUID, bool]
    confirmations: dict[tuple[str, UUID, UUID], models.Confirmation]
    awards: dict[tuple[str, UUID], models.FactAward]


async def _area_names(session: AsyncSession) -> dict[str, str]:
    return {
        key: name for key, name in await session.execute(select(models.Area.key, models.Area.name))
    }


async def _lookups(
    session: AsyncSession,
    viewer: models.User,
    target_id: UUID,
    history: MemberHistory,
    area_names: dict[str, str],
) -> _Lookups:
    event_ids = {
        fact.context.event.id
        for fact in history.facts
        if fact.type == FactType.ATTENDANCE and fact.context.event is not None
    }
    project_ids = {
        fact.context.project.id
        for fact in history.facts
        if fact.type == FactType.PROJECT_MEMBER and fact.context.project is not None
    }
    event_statuses = {
        event_id: status
        for event_id, status in await session.execute(
            select(models.Event.id, models.Event.status).where(models.Event.id.in_(event_ids))
        )
    }
    member_of = set(
        await session.scalars(
            select(models.ProjectMember.project_id).where(
                models.ProjectMember.project_id.in_(project_ids),
                models.ProjectMember.user_id == viewer.id,
                models.ProjectMember.state == MemberState.CONFIRMED,
                models.ProjectMember.left_at.is_(None),
            )
        )
    )
    project_visible = {
        project_id: visibility != ProjectVisibility.MEMBERS
        or viewer.is_admin
        or lead_user_id == viewer.id
        or project_id in member_of
        for project_id, visibility, lead_user_id in await session.execute(
            select(models.Project.id, models.Project.visibility, models.Project.lead_user_id).where(
                models.Project.id.in_(project_ids)
            )
        )
    }
    confirmations = {
        (row.fact_type, row.fact_id, row.id): row
        for row in await session.scalars(
            select(models.Confirmation).where(models.Confirmation.subject_user_id == target_id)
        )
    }
    awards = {
        (row.fact_type, row.fact_id): row
        for row in await session.scalars(
            select(models.FactAward).where(models.FactAward.user_id == target_id)
        )
    }
    return _Lookups(area_names, event_statuses, project_visible, confirmations, awards)


def _visible(fact: Fact, lookups: _Lookups) -> bool:
    if fact.flag_status == FlagStatus.TO_CHECK or fact.excluded:
        return False
    context = fact.context
    if fact.type == FactType.ATTENDANCE:
        return (
            context.event is not None
            and lookups.event_statuses.get(context.event.id) in VISIBLE_EVENT_STATUSES
        )
    if fact.type == FactType.PROJECT_MEMBER:
        return context.project is not None and lookups.project_visible.get(
            context.project.id, False
        )
    if fact.type == FactType.MENTORSHIP_SIDE:
        return (
            context.mentorship is not None
            and context.mentorship.status != MentorshipStatus.CANCELLED
        )
    return True


def _event_ref(fact: Fact) -> EventRef | None:
    event = fact.context.event
    if event is None:
        return None
    return EventRef(
        id=event.id,
        title=event.title,
        kind=event.kind,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        timezone=event.timezone,
    )


def _build_views(
    viewer: models.User,
    target_id: UUID,
    history: MemberHistory,
    lookups: _Lookups,
    *,
    now: datetime,
    withdraw_days: int,
    confirmable: set[FactKey],
    only: set[FactKey] | None,
) -> list[schemas.Fact]:
    entries = {entry.id: entry for entry in history.activity_entries}
    views: list[schemas.Fact] = []
    for fact in history.facts:
        if only is not None and (fact.type, fact.id) not in only:
            continue
        if not _visible(fact, lookups):
            continue
        context = fact.context
        description_md: str | None = None
        links: list[HttpUrl] = []
        activity_type = None
        event = None
        project = None
        if fact.type == FactType.ATTENDANCE and context.event is not None:
            subject_type = "event"
            subject_id = context.event.id
            title = context.event.title
            description_md = context.event.description_md
            occurred_on = context.event.starts_at.date()
            event = _event_ref(fact)
        elif fact.type == FactType.PROJECT_MEMBER and context.project is not None:
            subject_type = "project"
            subject_id = context.project.id
            title = context.project.name
            description_md = context.project.description_md
            links = [HttpUrl(str(link)) for link in context.project.links]
            occurred_on = fact.started_at.date()
            project = ProjectRef(
                id=context.project.id,
                name=context.project.name,
                event_id=context.project.event_id,
                team_status=context.project.team_status,
            )
            event = _event_ref(fact)
        elif fact.type == FactType.MENTORSHIP_SIDE and context.mentorship is not None:
            subject_type = "mentorship"
            subject_id = context.mentorship.id
            title = f"Mentorship in {fact.area or context.mentorship.area}"
            occurred_on = context.mentorship.started_at.date()
        elif fact.type == FactType.ACTIVITY_ENTRY and fact.id in entries:
            entry = entries[fact.id]
            subject_type = "activity_entry"
            subject_id = entry.id
            title = entry.title
            description_md = entry.description_md
            links = [HttpUrl(str(link)) for link in entry.links]
            activity_type = entry.type
            occurred_on = entry.occurred_on
        else:
            continue
        confirmations: list[schemas.Confirmation] = []
        if fact.type != FactType.ACTIVITY_ENTRY:
            confirmation_type = ConfirmationFactType(fact.type.value)
            for item in fact.confirmations:
                row = lookups.confirmations.get((confirmation_type.value, fact.id, item.id))
                confirmations.append(
                    schemas.Confirmation(
                        id=item.id,
                        fact_type=confirmation_type,
                        fact_id=fact.id,
                        subject_user_id=target_id,
                        confirmer_id=item.confirmer_id,
                        source=item.source,
                        ignored=item.ignored,
                        created_at=item.created_at,
                        withdrawn_at=item.withdrawn_at,
                        allowed_actions=[schemas.ConfirmationAction.WITHDRAW]
                        if row is not None and withdraw_allowed(row, viewer.id, now, withdraw_days)
                        else [],
                    )
                )
        award_row = lookups.awards.get((fact.type.value, fact.id))
        award = (
            schemas.FactAward(
                fact_type=FactType(award_row.fact_type),
                fact_id=award_row.fact_id,
                user_id=award_row.user_id,
                xp=float(award_row.xp),
                activity=float(award_row.activity),
                reason=award_row.reason,
                algo_version=award_row.algo_version,
                computed_at=award_row.computed_at,
            )
            if award_row is not None
            else None
        )
        area = (
            AreaRef(key=fact.area, name=lookups.area_names[fact.area])
            if fact.area is not None and fact.area in lookups.area_names
            else None
        )
        views.append(
            schemas.Fact(
                id=fact.id,
                type=fact.type,
                user_id=target_id,
                subject_type=subject_type,
                subject_id=subject_id,
                title=title,
                description_md=description_md,
                activity_type=activity_type,
                links=links,
                proof_links=[HttpUrl(str(link)) for link in fact.proof_links],
                note=fact.note or None,
                occurred_on=occurred_on,
                event=event,
                project=project,
                role=fact.role,
                area=area,
                trust=fact.trust,
                flag_status=fact.flag_status,
                confirmations=confirmations,
                award=award,
                created_at=fact.created_at,
                started_at=fact.started_at,
                ended_at=fact.ended_at,
                confirmed_at=fact.confirmed_at,
                left_at=fact.left_at,
                outcome=fact.outcome,
                allowed_actions=[schemas.FactAction.CONFIRM]
                if (fact.type, fact.id) in confirmable
                else [],
            )
        )
    return views


def _sort_key(view: schemas.Fact) -> tuple[datetime, str]:
    return (view.started_at, str(view.id))


async def list_user_facts(
    session: AsyncSession,
    viewer: models.User,
    target_id: UUID,
    params: CursorParams,
    *,
    fact_type: FactType | None,
    now: datetime,
) -> schemas.FactPage:
    target = await session.get(models.User, target_id)
    if target is None or target.onboarding_status not in ONBOARDED:
        raise ProblemError(ErrorCode.NOT_FOUND)
    cursor = (
        decode_cursor(params.cursor, [models.EventAttendance.created_at, models.EventAttendance.id])
        if params.cursor is not None
        else None
    )
    history = await load_member_history(session, target.id, now)
    settings = await load_settings(session)
    lookups = await _lookups(session, viewer, target.id, history, await _area_names(session))
    confirmable: set[FactKey] = set()
    if viewer.id != target.id:
        confirmable = {
            (FactType(candidate.fact_type.value), candidate.fact_id)
            for candidate in await confirmation_candidates(session, viewer.id, now)
            if candidate.subject_user_id == target.id
        }
    views = _build_views(
        viewer,
        target.id,
        history,
        lookups,
        now=now,
        withdraw_days=settings.confirmation_withdraw_days,
        confirmable=confirmable,
        only=None,
    )
    if fact_type is not None:
        views = [view for view in views if view.type == fact_type]
    views.sort(key=_sort_key, reverse=True)
    if cursor is not None:
        started_at, fact_id = cursor
        views = [
            view
            for view in views
            if view.started_at < started_at
            or (view.started_at == started_at and str(view.id) < str(fact_id))
        ]
    items = views[: params.limit + 1]
    next_cursor = None
    if len(items) > params.limit:
        items = items[: params.limit]
        last = items[-1]
        next_cursor = encode_cursor([last.started_at, last.id])
    return schemas.FactPage(items=items, next_cursor=next_cursor)


async def list_pending_confirmations(
    session: AsyncSession, viewer: models.User, now: datetime
) -> list[schemas.PendingConfirmationGroup]:
    candidates = await confirmation_candidates(session, viewer.id, now)
    if not candidates:
        return []
    titles: dict[tuple[str, UUID], str] = {}
    group_keys: dict[tuple[str, UUID], list[tuple[UUID, FactKey]]] = defaultdict(list)
    subject_keys: dict[UUID, set[FactKey]] = defaultdict(set)
    for candidate in candidates:
        group = (candidate.subject_type, candidate.subject_id)
        key = (FactType(candidate.fact_type.value), candidate.fact_id)
        titles.setdefault(group, candidate.title)
        group_keys[group].append((candidate.subject_user_id, key))
        subject_keys[candidate.subject_user_id].add(key)
    settings = await load_settings(session)
    area_names = await _area_names(session)
    views: dict[tuple[UUID, FactKey], schemas.Fact] = {}
    for subject_user_id in sorted(subject_keys, key=str):
        try:
            history = await load_member_history(session, subject_user_id, now)
        except ProblemError as error:
            if error.code != ErrorCode.NOT_FOUND:
                raise
            continue
        keys = subject_keys[subject_user_id]
        lookups = await _lookups(session, viewer, subject_user_id, history, area_names)
        for view in _build_views(
            viewer,
            subject_user_id,
            history,
            lookups,
            now=now,
            withdraw_days=settings.confirmation_withdraw_days,
            confirmable=keys,
            only=keys,
        ):
            views[(subject_user_id, (view.type, view.id))] = view.model_copy(
                update={"allowed_actions": [schemas.FactAction.CONFIRM]}
            )
    groups: list[schemas.PendingConfirmationGroup] = []
    for group in sorted(group_keys, key=lambda item: (item[0], titles[item], str(item[1]))):
        facts = sorted((views[item] for item in group_keys[group] if item in views), key=_sort_key)
        if not facts:
            continue
        groups.append(
            schemas.PendingConfirmationGroup(
                subject_type=group[0], subject_id=group[1], title=titles[group], facts=facts
            )
        )
    return groups
