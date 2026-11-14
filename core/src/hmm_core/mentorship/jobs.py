import hashlib
import json
from collections.abc import Sequence
from uuid import UUID

from hmm_contracts.algo import Area, MentorMatchRequest, Rec
from hmm_contracts.common import (
    AreaKey,
    HelpRequestFormat,
    HelpRequestStatus,
    MentorshipStatus,
    RecommendationSubjectType,
    RestrictionKind,
)
from hmm_contracts.notifications import AlertKind, HelpRequestMatchPayload, LinkAction
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import exists, func, or_, select

from hmm_core import models
from hmm_core.identity.service import ONBOARDED
from hmm_core.mentorship import views
from hmm_core.notifications.service import notify, raise_alert
from hmm_core.platform.algo import call_algo
from hmm_core.platform.jobs import JobName, Runtime, job
from hmm_core.platform.settings import load_settings
from hmm_core.recommendations import service as recommendations
from hmm_core.scores.snapshot import build_member_snapshots

CANDIDATE_LIMIT = 200
REASON_MAX = 200

area_key_adapter: TypeAdapter[str] = TypeAdapter(AreaKey)


def suggested_area(value: object) -> str | None:
    try:
        return area_key_adapter.validate_python(value)
    except ValidationError:
        return None


def validated_matches(
    items: Sequence[Rec], candidate_ids: frozenset[UUID], limit: int
) -> list[Rec]:
    normalized = [
        Rec.model_construct(
            user_id=item.user_id,
            score=item.score,
            reasons=[
                reason.strip()[:REASON_MAX]
                for reason in item.reasons
                if isinstance(reason, str) and reason.strip()
            ],
            tags=item.tags,
            suggested_area=suggested_area(item.suggested_area),
        )
        for item in items
    ]
    kept, _ = recommendations.validated_group(normalized, candidate_ids)
    return sorted(kept, key=lambda rec: (-rec.score, rec.user_id))[:limit]


@job(JobName.MATCH_MENTORS)
async def match_mentors(runtime: Runtime, request_id: str) -> None:
    now = runtime.clock.now()
    async with runtime.sessionmaker.begin() as session:
        request = await session.get(models.HelpRequest, UUID(request_id))
        if (
            request is None
            or request.status != HelpRequestStatus.OPEN.value
            or request.expires_at <= now
        ):
            return None
        learner = await session.get(models.User, request.learner_id)
        if learner is None or learner.banned_at is not None:
            return None
        area = await session.get(models.Area, request.area_id)
        if area is None:
            return None
        settings = await load_settings(session)
        live = (
            select(
                models.Mentorship.mentor_id.label("user_id"),
                func.count().label("active_count"),
            )
            .where(models.Mentorship.status == MentorshipStatus.ACTIVE.value)
            .group_by(models.Mentorship.mentor_id)
            .subquery("mentor_load")
        )
        offer = exists().where(
            models.MentorOffer.user_id == models.User.id,
            models.MentorOffer.area_id == area.id,
            models.MentorOffer.active.is_(True),
        )
        scored = exists().where(
            models.AreaScore.user_id == models.User.id,
            models.AreaScore.area_id == area.id,
        )
        effective = (
            select(func.coalesce(models.AreaScore.override, models.AreaScore.calculated))
            .where(
                models.AreaScore.user_id == models.User.id,
                models.AreaScore.area_id == area.id,
            )
            .correlate(models.User)
            .scalar_subquery()
        )
        restricted = select(models.MemberRestriction.user_id).where(
            models.MemberRestriction.kind == RestrictionKind.MENTORING.value,
            models.MemberRestriction.lifted_at.is_(None),
            or_(models.MemberRestriction.until.is_(None), models.MemberRestriction.until > now),
        )
        statement = (
            select(models.User.id)
            .select_from(models.User)
            .outerjoin(live, live.c.user_id == models.User.id)
            .where(
                models.User.id != learner.id,
                models.User.onboarding_status.in_(ONBOARDED),
                models.User.banned_at.is_(None),
                models.User.dnd_indefinite.is_(False),
                or_(models.User.dnd_until.is_(None), models.User.dnd_until <= now),
                models.User.id.not_in(restricted),
                func.coalesce(live.c.active_count, 0)
                < func.coalesce(models.User.mentor_capacity, settings.mentor_capacity_default),
                or_(offer, scored),
            )
            .order_by(offer.desc(), func.coalesce(effective, 0).desc(), models.User.id)
            .limit(CANDIDATE_LIMIT)
        )
        candidate_ids = list(await session.scalars(statement))
        snapshots = await build_member_snapshots(session, [learner.id, *candidate_ids], now)
        if learner.id not in snapshots:
            return None
        payload = MentorMatchRequest(
            area=Area(
                id=area.id,
                key=area.key,
                name=area.name,
                description=area.description,
                sort=area.sort,
                archived_at=area.archived_at,
            ),
            topic=request.topic,
            goal=request.goal,
            format=HelpRequestFormat(request.format),
            learner=snapshots[learner.id],
            candidates=[snapshots[cid] for cid in candidate_ids if cid in snapshots],
            limit=settings.mentor_notify_top_n,
        )
        algo_version = runtime.algorithm.version
        input_hash = hashlib.sha256(
            json.dumps(
                {"request": payload.model_dump(mode="json"), "algo_version": algo_version},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        ctx = await runtime.algo_context(session)
        try:
            raw = await call_algo("match_mentors", runtime.algorithm.match_mentors(ctx, payload))
        except Exception as error:
            await recommendations.mark_recommendation_set_failed(
                session,
                subject_type=RecommendationSubjectType.HELP_REQUEST,
                subject_id=request.id,
                input_hash=input_hash,
                algo_version=algo_version,
                error=str(error)[:1000],
            )
            await raise_alert(
                session,
                AlertKind.ALGO_FAILURE,
                now,
                title="Mentor matching failed",
                message=f"match_mentors failed for help request {request.id}: {error}",
                link="/admin/system",
                subject_type="help_request",
                subject_id=request.id,
                subject_title=request.topic,
                dedupe_key=f"algo_failure:match_mentors:{request.id}:{now:%Y-%m-%d-%H}",
            )
            runtime.log.warning("match_mentors failed for help request %s: %s", request.id, error)
            return None
        recs = validated_matches(
            raw,
            frozenset(member.user_id for member in payload.candidates),
            settings.mentor_notify_top_n,
        )
        await recommendations.replace_recommendation_set(
            session,
            subject_type=RecommendationSubjectType.HELP_REQUEST,
            subject_id=request.id,
            input_hash=input_hash,
            algo_version=algo_version,
            enrolled=None,
            others=recs,
            now=now,
        )
        for rec in recs:
            await notify(
                session,
                rec.user_id,
                HelpRequestMatchPayload(
                    help_request_id=request.id,
                    learner=views.notification_person(learner),
                    area_key=area.key,
                    area_name=area.name,
                    topic=request.topic,
                    goal=request.goal,
                    format=HelpRequestFormat(request.format),
                    expires_at=request.expires_at,
                    reason=rec.reasons[0],
                    link=f"/help-requests/{request.id}",
                    actions=(
                        LinkAction(
                            kind="link", label_key="notifications.actions.help", url="/mentorship"
                        ),
                    ),
                ),
                now,
                dedupe_key=f"help_request_match:{request.id}:{rec.user_id}",
            )
    return None
