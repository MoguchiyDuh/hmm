from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from hmm_contracts.common import (
    ConfirmationFactType,
    LeadOfferSource,
    LeadOfferStatus,
    MemberState,
    ProjectStatus,
)
from hmm_contracts.notifications import (
    NotificationPerson,
    ProjectLeadClaimablePayload,
    ProjectLeadOfferPayload,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.notifications.service import notify, notify_many
from hmm_core.platform.settings import load_settings
from hmm_core.projects.common import project_member_ids


async def standalone_lead_succession(
    session: AsyncSession, project: models.Project, now: datetime, *, exclude: set[UUID]
) -> None:
    from hmm_core.formation import service as formation

    if project.lead_user_id is not None:
        return None
    project.lead_succession_excluded = sorted(
        {*project.lead_succession_excluded, *exclude}, key=str
    )
    await formation.close_pending_lead_offers(session, project.id, now)
    excluded = set(project.lead_succession_excluded)
    candidates = [
        candidate
        for candidate in await standalone_lead_order(session, project)
        if candidate.user.id not in excluded
    ]
    if not candidates:
        await pause_project_for_lead(session, project, now)
        return None
    expires_at = now + timedelta(days=(await load_settings(session)).project_lead_offer_days)
    offer = models.LeadOffer(
        project_id=project.id,
        event_id=None,
        user_id=candidates[0].user.id,
        source=LeadOfferSource.SUCCESSION.value,
        offered_by=None,
        tier=None,
        reason="The project needs a lead",
        status=LeadOfferStatus.PENDING.value,
        expires_at=expires_at,
        responded_at=None,
    )
    session.add(offer)
    await session.flush()
    await session.refresh(project)
    await notify(
        session,
        offer.user_id,
        project_lead_offer_payload(project, offer, None),
        now,
        dedupe_key=f"project_lead_offer:{offer.id}",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectLeadCandidate:
    member: models.ProjectMember
    user: models.User
    contributions: int


async def standalone_lead_order(
    session: AsyncSession, project: models.Project
) -> list[ProjectLeadCandidate]:
    rows = list(
        await session.execute(
            select(models.ProjectMember, models.User)
            .join(models.User, models.User.id == models.ProjectMember.user_id)
            .where(
                models.ProjectMember.project_id == project.id,
                models.ProjectMember.left_at.is_(None),
                models.ProjectMember.is_team_row.is_(False),
                models.ProjectMember.state == MemberState.CONFIRMED.value,
                models.User.banned_at.is_(None),
            )
        )
    )
    member_ids = [member.id for member, _ in rows]
    contributions = (
        {
            fact_id: count
            for fact_id, count in await session.execute(
                select(models.Confirmation.fact_id, func.count())
                .where(
                    models.Confirmation.fact_type == ConfirmationFactType.PROJECT_MEMBER.value,
                    models.Confirmation.fact_id.in_(member_ids),
                    models.Confirmation.ignored.is_(False),
                    models.Confirmation.withdrawn_at.is_(None),
                    models.Confirmation.confirmer_id.is_not(None),
                )
                .group_by(models.Confirmation.fact_id)
            )
        }
        if member_ids
        else {}
    )
    return sorted(
        [
            ProjectLeadCandidate(
                member=member, user=user, contributions=contributions.get(member.id, 0)
            )
            for member, user in rows
        ],
        key=lambda candidate: (
            -candidate.contributions,
            candidate.member.joined_at,
            str(candidate.user.id),
        ),
    )


async def pause_project_for_lead(
    session: AsyncSession, project: models.Project, now: datetime
) -> None:
    project.status = ProjectStatus.PAUSED.value
    project.lead_user_id = None
    project.last_lead_action_at = now
    await session.flush()
    await session.refresh(project)
    member_ids = await project_member_ids(session, project.id)
    if member_ids:
        await notify_many(
            session,
            member_ids,
            ProjectLeadClaimablePayload(
                link=f"/projects/{project.id}",
                project_id=project.id,
                project_name=project.name,
                member_count=len(member_ids),
                reason="Nobody accepted the lead role",
            ),
            now,
            dedupe_key=f"project_lead_claimable:{project.id}:{int(now.timestamp())}",
        )


def project_lead_offer_payload(
    project: models.Project, offer: models.LeadOffer, offerer: models.User | None
) -> ProjectLeadOfferPayload:
    return ProjectLeadOfferPayload(
        link=f"/projects/{project.id}",
        project_id=project.id,
        project_name=project.name,
        lead_offer_id=offer.id,
        source=LeadOfferSource(offer.source),
        offered_by=NotificationPerson(user_id=offerer.id, display_name=offerer.display_name)
        if offerer is not None
        else None,
        expires_at=offer.expires_at,
    )


async def expire_project_lead_offers(
    session: AsyncSession, now: datetime, *, limit: int = 50
) -> int:
    offers = list(
        await session.scalars(
            select(models.LeadOffer)
            .where(
                models.LeadOffer.status == LeadOfferStatus.PENDING.value,
                models.LeadOffer.expires_at <= now,
                models.LeadOffer.event_id.is_(None),
            )
            .order_by(models.LeadOffer.expires_at, models.LeadOffer.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    processed = 0
    for offer in offers:
        project = await session.scalar(
            select(models.Project)
            .where(models.Project.id == offer.project_id, models.Project.event_id.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if project is None:
            continue
        if offer.status != LeadOfferStatus.PENDING.value or offer.expires_at > now:
            continue
        offer.status = LeadOfferStatus.EXPIRED.value
        offer.responded_at = now
        await session.flush()
        processed += 1
        if offer.source != LeadOfferSource.TRANSFER.value and project.lead_user_id is None:
            await standalone_lead_succession(session, project, now, exclude={offer.user_id})
    return processed
