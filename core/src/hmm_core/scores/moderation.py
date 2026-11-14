from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import pydantic
from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    ErrorCode,
    FlagStatus,
    FlagSubjectType,
    Severity,
)
from hmm_contracts.notifications import PenaltyAppliedPayload
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.notifications.service import notify
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.pagination import CursorParams, keyset_page
from hmm_core.platform.schemas import UserRef
from hmm_core.scores import schemas
from hmm_core.scores.schemas import CreateXpAdjustmentBody, FlagAction, FlagPenalties

type Content = dict[str, JsonValue]


async def create_xp_adjustment(
    session: AsyncSession, user_id: UUID, body: CreateXpAdjustmentBody, actor: UUID, now: datetime
) -> models.XpAdjustment:
    if await session.get(models.User, user_id) is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    amount = Decimal(str(body.amount))
    row = models.XpAdjustment(
        user_id=user_id, amount=amount, reason=body.reason, admin_id=actor, flag_id=None
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    await record_audit(
        session,
        actor=actor,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.XP_ADJUST,
        entity_type=AuditEntityType.XP_ADJUSTMENT,
        entity_id=row.id,
        after={"user_id": str(user_id), "amount": float(amount), "reason": body.reason},
        reason=body.reason,
    )
    if amount < 0:
        await notify(
            session,
            user_id,
            PenaltyAppliedPayload(
                link=f"/users/{user_id}",
                user_id=user_id,
                reason=body.reason,
                xp_delta=int(amount),
            ),
            now,
        )
    return row


async def _attendance_content(session: AsyncSession, ids: list[UUID]) -> dict[UUID, Content]:
    rows = await session.execute(
        select(models.EventAttendance, models.Event)
        .join(models.Event, models.Event.id == models.EventAttendance.event_id)
        .where(models.EventAttendance.id.in_(ids))
    )
    return {
        attendance.id: {
            "event_id": str(event.id),
            "event_title": event.title,
            "event_slug": event.slug,
            "role": attendance.role,
            "status": attendance.status,
        }
        for attendance, event in rows
    }


async def _project_member_content(session: AsyncSession, ids: list[UUID]) -> dict[UUID, Content]:
    rows = await session.execute(
        select(models.ProjectMember, models.Project)
        .join(models.Project, models.Project.id == models.ProjectMember.project_id)
        .where(models.ProjectMember.id.in_(ids))
    )
    return {
        member.id: {
            "project_id": str(project.id),
            "project_name": project.name,
            "role": member.role,
            "state": member.state,
        }
        for member, project in rows
    }


async def _mentorship_content(session: AsyncSession, ids: list[UUID]) -> dict[UUID, Content]:
    rows = await session.execute(
        select(models.Mentorship, models.Area.key)
        .join(models.Area, models.Area.id == models.Mentorship.area_id)
        .where(models.Mentorship.id.in_(ids))
    )
    return {
        mentorship.id: {
            "mentorship_id": str(mentorship.id),
            "mentor_id": str(mentorship.mentor_id),
            "learner_id": str(mentorship.learner_id),
            "area_key": area_key,
            "kind": mentorship.kind,
            "status": mentorship.status,
        }
        for mentorship, area_key in rows
    }


async def _confirmation_content(session: AsyncSession, ids: list[UUID]) -> dict[UUID, Content]:
    rows = await session.scalars(select(models.Confirmation).where(models.Confirmation.id.in_(ids)))
    return {
        row.id: {
            "confirmation_id": str(row.id),
            "fact_type": row.fact_type,
            "fact_id": str(row.fact_id),
            "confirmer_id": None if row.confirmer_id is None else str(row.confirmer_id),
            "source": row.source,
            "subject_user_id": str(row.subject_user_id),
        }
        for row in rows
    }


async def _activity_entry_content(session: AsyncSession, ids: list[UUID]) -> dict[UUID, Content]:
    rows = await session.scalars(
        select(models.ActivityEntry).where(models.ActivityEntry.id.in_(ids))
    )
    return {
        row.id: {
            "entry_id": str(row.id),
            "type": row.type,
            "title": row.title,
            "occurred_on": row.occurred_on.isoformat(),
        }
        for row in rows
    }


async def _review_content(session: AsyncSession, ids: list[UUID]) -> dict[UUID, Content]:
    rows = await session.scalars(select(models.Review).where(models.Review.id.in_(ids)))
    return {
        row.id: {
            "review_id": str(row.id),
            "context": row.context,
            "rating": row.rating,
            "reviewer_id": str(row.reviewer_id),
            "reviewee_id": str(row.reviewee_id),
            "subject_type": row.subject_type,
            "subject_id": str(row.subject_id),
        }
        for row in rows
    }


async def _profile_content(session: AsyncSession, ids: list[UUID]) -> dict[UUID, Content]:
    rows = await session.scalars(select(models.User).where(models.User.id.in_(ids)))
    return {
        row.id: {
            "user_id": str(row.id),
            "display_name": row.display_name,
            "banned": row.banned_at is not None,
        }
        for row in rows
    }


_CONTENT_LOADERS = {
    FlagSubjectType.ATTENDANCE: _attendance_content,
    FlagSubjectType.PROJECT_MEMBER: _project_member_content,
    FlagSubjectType.MENTORSHIP: _mentorship_content,
    FlagSubjectType.CONFIRMATION: _confirmation_content,
    FlagSubjectType.ACTIVITY_ENTRY: _activity_entry_content,
    FlagSubjectType.REVIEW: _review_content,
    FlagSubjectType.PROFILE: _profile_content,
}


async def _subject_contents(
    session: AsyncSession, rows: Sequence[models.Flag]
) -> dict[UUID, Content]:
    grouped: dict[FlagSubjectType, list[UUID]] = {}
    for row in rows:
        grouped.setdefault(FlagSubjectType(row.subject_type), []).append(row.subject_id)
    loaded: dict[tuple[FlagSubjectType, UUID], Content] = {}
    for subject_type, ids in grouped.items():
        loader = _CONTENT_LOADERS.get(subject_type)
        if loader is None:
            continue
        for subject_id, content in (await loader(session, list(dict.fromkeys(ids)))).items():
            loaded[(subject_type, subject_id)] = content
    contents: dict[UUID, Content] = {}
    for row in rows:
        subject_type = FlagSubjectType(row.subject_type)
        if subject_type is FlagSubjectType.RING:
            contents[row.id] = {"member_ids": [str(uid) for uid in sorted(row.related_user_ids)]}
        else:
            contents[row.id] = loaded.get((subject_type, row.subject_id), {})
    return contents


def _penalty(value: JsonValue | None) -> FlagPenalties | None:
    if not isinstance(value, dict):
        return None
    try:
        return FlagPenalties.model_validate(value)
    except pydantic.ValidationError:
        return None


def _user_ref(user: models.User) -> UserRef:
    try:
        return UserRef(id=user.id, display_name=user.display_name, avatar_url=user.avatar_url)
    except pydantic.ValidationError:
        return UserRef(id=user.id, display_name=user.display_name, avatar_url=None)


async def flag_views(session: AsyncSession, rows: Sequence[models.Flag]) -> list[schemas.Flag]:
    related_ids = {uid for row in rows for uid in row.related_user_ids}
    users = (
        {
            user.id: user
            for user in await session.scalars(
                select(models.User).where(models.User.id.in_(related_ids))
            )
        }
        if related_ids
        else {}
    )
    contents = await _subject_contents(session, rows)
    views: list[schemas.Flag] = []
    for row in rows:
        related = sorted(
            (users[uid] for uid in dict.fromkeys(row.related_user_ids) if uid in users),
            key=lambda user: (user.display_name, str(user.id)),
        )
        views.append(
            schemas.Flag(
                id=row.id,
                user_id=row.user_id,
                subject_type=FlagSubjectType(row.subject_type),
                subject_id=row.subject_id,
                subject_content=contents[row.id],
                related_user_ids=list(row.related_user_ids),
                related_members=[_user_ref(user) for user in related],
                reason=row.reason,
                severity=Severity(row.severity),
                status=FlagStatus(row.status),
                algo_version=row.algo_version,
                resolved_by=row.resolved_by,
                resolved_at=row.resolved_at,
                resolution_note=row.resolution_note,
                penalty=_penalty(row.penalty),
                created_at=row.created_at,
                allowed_actions=[FlagAction.RESOLVE]
                if row.status == FlagStatus.TO_CHECK.value
                else [],
            )
        )
    return views


async def list_flags(
    session: AsyncSession,
    pagination: CursorParams,
    *,
    status: FlagStatus,
    user_id: UUID | None,
    subject_type: FlagSubjectType | None,
) -> schemas.FlagPage:
    statement = select(models.Flag).where(models.Flag.status == status.value)
    if user_id is not None:
        statement = statement.where(models.Flag.user_id == user_id)
    if subject_type is not None:
        statement = statement.where(models.Flag.subject_type == subject_type.value)
    page = await keyset_page(
        session,
        statement,
        pagination,
        sort=[models.Flag.created_at],
        id_column=models.Flag.id,
        serialize=lambda row: row,
    )
    return schemas.FlagPage(
        items=await flag_views(session, page.items), next_cursor=page.next_cursor
    )
