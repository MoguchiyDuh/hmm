from datetime import datetime
from uuid import UUID

from hmm_contracts.common import (
    ActorKind,
    AuditAction,
    AuditEntityType,
    CvTextStatus,
    ErrorCode,
    MemberReviewStatus,
)
from hmm_contracts.notifications import NotificationPerson, ScoreChange, ScoresReviewedPayload
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.notifications.service import notify
from hmm_core.onboarding import schemas
from hmm_core.onboarding.schemas import MemberReviewAction
from hmm_core.onboarding.submissions import user_onboarding_history
from hmm_core.platform.audit import record_audit
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.pagination import CursorParams, keyset_page
from hmm_core.platform.schemas import UserRef
from hmm_core.platform.storage import ObjectStore
from hmm_core.profiles import service as profiles
from hmm_core.profiles.github import github_summary
from hmm_core.scores import service as scores_service


def review_actions(review: models.MemberReview) -> list[schemas.MemberReviewAction]:
    actions = [MemberReviewAction.EDIT]
    if review.status == MemberReviewStatus.PENDING:
        actions.append(MemberReviewAction.COMPLETE)
    return actions


async def require_review(
    session: AsyncSession, user_id: UUID, *, for_update: bool = False
) -> models.MemberReview:
    statement = select(models.MemberReview).where(models.MemberReview.user_id == user_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    review = await session.scalar(statement)
    if review is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return review


async def member_review_view(
    session: AsyncSession, review: models.MemberReview
) -> schemas.MemberReview:
    user = await session.get(models.User, review.user_id)
    if user is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return schemas.MemberReview(
        user=UserRef(id=user.id, display_name=user.display_name, avatar_url=user.avatar_url),
        status=MemberReviewStatus(review.status),
        opened_at=review.opened_at,
        reviewed_by=review.reviewed_by,
        reviewed_at=review.reviewed_at,
        admin_note=review.admin_note,
        discrepancies=[str(item) for item in review.discrepancies]
        if isinstance(review.discrepancies, list)
        else [],
        allowed_actions=review_actions(review),
    )


async def member_reviews_page(
    session: AsyncSession, pagination: CursorParams, status: MemberReviewStatus | None
) -> schemas.MemberReviewPage:
    statement = select(models.MemberReview)
    if status is not None:
        statement = statement.where(models.MemberReview.status == status.value)
    page = await keyset_page(
        session,
        statement,
        pagination,
        sort=[models.MemberReview.opened_at],
        id_column=models.MemberReview.user_id,
        serialize=lambda row: row,
        descending=False,
    )
    return schemas.MemberReviewPage(
        items=[await member_review_view(session, review) for review in page.items],
        next_cursor=page.next_cursor,
    )


async def member_review_detail(
    session: AsyncSession, store: ObjectStore | None, user_id: UUID, actor: models.User
) -> schemas.MemberReviewDetail:
    review = await require_review(session, user_id)
    base = await member_review_view(session, review)
    submissions = await user_onboarding_history(session, user_id)
    cv: schemas.ReviewCv | None = None
    if await session.get(models.CvFile, user_id) is not None:
        row, url = await profiles.cv_download(
            session, profiles.require_storage(store), actor=actor, user_id=user_id
        )
        cv = schemas.ReviewCv(
            filename=row.filename,
            url=url,
            text=row.text,
            text_status=CvTextStatus(row.text_status),
            text_error=row.text_error,
        )
    github = github_summary(await session.get(models.GithubSnapshot, user_id))
    area_scores = await scores_service.list_scores(session, user_id, actor)
    return schemas.MemberReviewDetail(
        user=base.user,
        status=base.status,
        opened_at=base.opened_at,
        reviewed_by=base.reviewed_by,
        reviewed_at=base.reviewed_at,
        admin_note=base.admin_note,
        discrepancies=base.discrepancies,
        allowed_actions=base.allowed_actions,
        submissions=submissions,
        cv=cv,
        github=github,
        area_scores=area_scores,
    )


async def update_member_review(
    session: AsyncSession, actor: models.User, user_id: UUID, note: str, reason: str | None
) -> schemas.MemberReview:
    review = await require_review(session, user_id, for_update=True)
    if review.admin_note == note:
        return await member_review_view(session, review)
    before: dict[str, JsonValue] = {"admin_note": review.admin_note}
    review.admin_note = note
    await session.flush()
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.MEMBER_REVIEW_EDIT,
        entity_type=AuditEntityType.MEMBER_REVIEW,
        entity_id=user_id,
        before=before,
        after={"admin_note": note},
        reason=reason,
    )
    return await member_review_view(session, review)


async def complete_member_review(
    session: AsyncSession,
    actor: models.User,
    user_id: UUID,
    note: str | None,
    reason: str | None,
    now: datetime,
) -> schemas.MemberReview:
    review = await require_review(session, user_id, for_update=True)
    if review.status == MemberReviewStatus.REVIEWED:
        return await member_review_view(session, review)
    before: dict[str, JsonValue] = {
        "status": "pending",
        "reviewed_by": str(review.reviewed_by) if review.reviewed_by is not None else None,
        "reviewed_at": review.reviewed_at.isoformat() if review.reviewed_at is not None else None,
        "admin_note": review.admin_note,
    }
    review.status = MemberReviewStatus.REVIEWED
    review.reviewed_by = actor.id
    review.reviewed_at = now
    if note is not None:
        review.admin_note = note
    await session.flush()
    await record_audit(
        session,
        actor=actor.id,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.MEMBER_REVIEW_COMPLETE,
        entity_type=AuditEntityType.MEMBER_REVIEW,
        entity_id=user_id,
        before=before,
        after={
            "status": "reviewed",
            "reviewed_by": str(actor.id),
            "reviewed_at": now.isoformat(),
            "admin_note": review.admin_note,
        },
        reason=reason,
    )
    rows = await session.execute(
        select(models.AreaScore, models.Area)
        .join(models.Area, models.Area.id == models.AreaScore.area_id)
        .where(models.AreaScore.user_id == user_id, models.Area.archived_at.is_(None))
        .order_by(models.Area.sort)
    )
    changes = tuple(
        ScoreChange(
            area_key=area.key,
            area_name=area.name,
            previous_score=float(score.calculated),
            score=float(score.override),
            reason=score.override_reason or score.calc_reason,
        )
        for score, area in rows
        if score.override is not None and score.override != score.calculated
    )
    await notify(
        session,
        user_id,
        ScoresReviewedPayload(
            link=f"/users/{user_id}",
            reviewer=NotificationPerson(user_id=actor.id, display_name=actor.display_name),
            changes=changes,
        ),
        now,
    )
    return await member_review_view(session, review)
