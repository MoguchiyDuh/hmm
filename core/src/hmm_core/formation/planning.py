from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from hmm_contracts.algo import (
    AlgoSettings,
    Core,
    EnrolledMember,
    FormationPlan,
    FormationRequest,
    MemberSnapshot,
    PlannedMember,
    PlannedMerge,
    PlannedTeam,
    RankedMember,
    TeamMemberSnapshot,
)
from hmm_contracts.algo import LeadFit as AlgoLeadFit
from hmm_contracts.common import (
    AreaImportance,
    AreaKey,
    EnrollmentStatus,
    ErrorCode,
    FormationKind,
    FormationPhase,
    MemberRole,
    MemberState,
    ProjectOrigin,
    TeamStatus,
)
from hmm_contracts.formation import placement_bounds
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models
from hmm_core.formation.common import LIVE_ENROLLMENT_STATES
from hmm_core.formation.lead_offers import lead_order
from hmm_core.formation.leads import formation_sizes, lead_candidate_tier
from hmm_core.platform.errors import ProblemError
from hmm_core.platform.settings import load_settings
from hmm_core.scores.snapshot import build_member_snapshots


async def preserved_projects(
    session: AsyncSession, event_id: UUID, kind: FormationKind, phase: FormationPhase
) -> list[models.Project]:
    if kind == FormationKind.LATE:
        scope = (
            models.Project.team_status == TeamStatus.FORMING.value,
            models.Project.origin == ProjectOrigin.LATE_CLAIM.value,
        )
    elif phase == FormationPhase.OPEN:
        scope = (models.Project.team_status == TeamStatus.FORMING.value,)
    elif phase == FormationPhase.REVIEW:
        scope = (models.Project.team_status == TeamStatus.REVIEW.value,)
    else:
        return []
    return list(
        await session.scalars(
            select(models.Project)
            .where(models.Project.event_id == event_id, *scope)
            .order_by(models.Project.created_at, models.Project.id)
        )
    )


@dataclass(frozen=True, slots=True)
class MemberFacts:
    willing_to_lead: bool
    enrolled_at: datetime
    fit: float
    tier: int


def formation_member_facts(
    request: FormationRequest, settings: AlgoSettings
) -> dict[UUID, MemberFacts]:
    members: list[TeamMemberSnapshot | EnrolledMember] = [
        snapshot for core in request.cores for snapshot in (core.lead, *core.members)
    ]
    members.extend(request.unplaced)
    facts: dict[UUID, MemberFacts] = {}
    for member in members:
        if member.user_id in facts:
            continue
        lead_fit = request.lead_fits.get(member.user_id)
        fit = lead_fit.fit if lead_fit is not None else 0.0
        facts[member.user_id] = MemberFacts(
            willing_to_lead=member.willing_to_lead,
            enrolled_at=member.enrolled_at,
            fit=fit,
            tier=lead_candidate_tier(member.user_id, fit, member.willing_to_lead, set(), settings),
        )
    return facts


def planned_lead_user(facts: dict[UUID, MemberFacts], user_ids: Sequence[UUID]) -> UUID | None:
    if not user_ids:
        return None
    return min(
        user_ids,
        key=lambda uid: (facts[uid].tier, -facts[uid].fit, facts[uid].enrolled_at, str(uid)),
    )


def _free_pool_statuses(kind: FormationKind, phase: FormationPhase) -> frozenset[str]:
    if kind == FormationKind.LATE:
        return frozenset({EnrollmentStatus.RESERVE.value})
    if phase == FormationPhase.OPEN:
        return frozenset({EnrollmentStatus.ACTIVE.value})
    if phase == FormationPhase.REVIEW:
        return frozenset({EnrollmentStatus.ACTIVE.value, EnrollmentStatus.RESERVE.value})
    return frozenset()


async def build_formation_request(
    session: AsyncSession,
    event: models.Event,
    formation: models.EventFormation,
    kind: FormationKind,
    phase: FormationPhase,
    now: datetime,
) -> FormationRequest:
    from hmm_core.catalog import service as catalog

    settings = await load_settings(session)
    sizes = formation_sizes(event, formation, settings)
    preserved = await preserved_projects(session, event.id, kind, phase)
    project_ids = [project.id for project in preserved]
    rows: list[models.ProjectMember] = (
        list(
            await session.scalars(
                select(models.ProjectMember)
                .where(
                    models.ProjectMember.project_id.in_(project_ids),
                    models.ProjectMember.left_at.is_(None),
                    models.ProjectMember.is_team_row.is_(True),
                )
                .order_by(models.ProjectMember.joined_at, models.ProjectMember.id)
            )
        )
        if project_ids
        else []
    )
    enrollments: dict[UUID, models.Enrollment] = {
        enrollment.user_id: enrollment
        for enrollment in await session.scalars(
            select(models.Enrollment)
            .where(
                models.Enrollment.event_id == event.id,
                models.Enrollment.status.in_(
                    sorted(status.value for status in LIVE_ENROLLMENT_STATES)
                ),
            )
            .order_by(models.Enrollment.created_at, models.Enrollment.id)
        )
    }
    user_ids = {row.user_id for row in rows} | set(enrollments)
    users: dict[UUID, models.User] = (
        {
            user.id: user
            for user in await session.scalars(
                select(models.User).where(models.User.id.in_(user_ids))
            )
        }
        if user_ids
        else {}
    )
    area_ids = {row.area_id for row in rows if row.area_id is not None} | {
        enrollment.preferred_area_id
        for enrollment in enrollments.values()
        if enrollment.preferred_area_id is not None
    }
    areas: dict[UUID, str] = (
        {
            area_id: key
            for area_id, key in await session.execute(
                select(models.Area.id, models.Area.key).where(models.Area.id.in_(area_ids))
            )
        }
        if area_ids
        else {}
    )

    def area_key(area_id: UUID | None) -> str | None:
        return areas.get(area_id) if area_id is not None else None

    members_by_project: dict[UUID, list[models.ProjectMember]] = {}
    for row in rows:
        user = users.get(row.user_id)
        if user is not None and user.banned_at is None:
            members_by_project.setdefault(row.project_id, []).append(row)
    core_user_ids = {row.user_id for members in members_by_project.values() for row in members}
    statuses = _free_pool_statuses(kind, phase)
    free = sorted(
        (
            enrollment
            for user_id, enrollment in enrollments.items()
            if enrollment.status in statuses
            and user_id in users
            and users[user_id].banned_at is None
            and user_id not in core_user_ids
        ),
        key=lambda enrollment: (enrollment.created_at, str(enrollment.user_id)),
    )
    snapshots = await build_member_snapshots(
        session, sorted(core_user_ids | {enrollment.user_id for enrollment in free}), now
    )

    def team_member(project: models.Project, member: models.ProjectMember) -> TeamMemberSnapshot:
        enrollment = enrollments.get(member.user_id)
        return TeamMemberSnapshot(
            **snapshots[member.user_id].model_dump(),
            role=MemberRole(member.role),
            area=area_key(member.area_id),
            state=MemberState(member.state),
            willing_to_lead=enrollment.willing_to_lead if enrollment is not None else False,
            claimed_lead=member.user_id == project.lead_user_id,
            enrolled_at=enrollment.created_at if enrollment is not None else member.joined_at,
        )

    cores: list[Core] = []
    for project in preserved:
        kept = [row for row in members_by_project.get(project.id, []) if row.user_id in snapshots]
        if not kept:
            continue
        kept_ids = {row.user_id for row in kept}
        if project.lead_user_id is not None and project.lead_user_id in kept_ids:
            lead_id = project.lead_user_id
        else:
            order = await lead_order(session, project, now)
            lead_id = next(
                (entry.user.id for entry in order if entry.user.id in kept_ids), kept[0].user_id
            )
        lead_row = next(row for row in kept if row.user_id == lead_id)
        cores.append(
            Core(
                project_id=project.id,
                lead=team_member(project, lead_row),
                members=[team_member(project, row) for row in kept if row.user_id != lead_id],
            )
        )
    unplaced = [
        EnrolledMember(
            **snapshots[enrollment.user_id].model_dump(),
            preferred_area=area_key(enrollment.preferred_area_id),
            willing_to_lead=enrollment.willing_to_lead,
            enrolled_at=enrollment.created_at,
            note=enrollment.note or "",
        )
        for enrollment in free
        if enrollment.user_id in snapshots
    ]
    try:
        bounds = placement_bounds(
            len(unplaced),
            [1 + len(core.members) for core in cores],
            sizes.viable,
            sizes.target,
            sizes.max_size,
        )
    except ValueError as error:
        raise ProblemError(
            ErrorCode.INVALID_STATE, "Formation sizes cannot accommodate the current cores"
        ) from error
    unplaced_ids = [member.user_id for member in unplaced]
    lead_fits: dict[UUID, AlgoLeadFit] = (
        {
            row.user_id: AlgoLeadFit(user_id=row.user_id, fit=float(row.fit), reason=row.reason)
            for row in await session.scalars(
                select(models.LeadFit).where(
                    models.LeadFit.event_id == event.id,
                    models.LeadFit.user_id.in_(unplaced_ids),
                )
            )
        }
        if unplaced_ids
        else {}
    )
    return FormationRequest(
        hackathon=await catalog.event_snapshot(session, event.id),
        kind=kind,
        target=sizes.target,
        viable=sizes.viable,
        max_size=sizes.max_size,
        bounds=bounds,
        cores=cores,
        unplaced=unplaced,
        lead_fits=lead_fits,
    )


def _violation(
    constraint: int,
    message: str,
    *,
    user_id: UUID | None = None,
    project_id: UUID | None = None,
) -> JsonValue:
    return {
        "constraint": constraint,
        "message": message,
        "user_id": str(user_id) if user_id is not None else None,
        "project_id": str(project_id) if project_id is not None else None,
    }


def validate_plan(
    request: FormationRequest, plan: FormationPlan, settings: AlgoSettings
) -> list[JsonValue]:
    errors: list[JsonValue] = []
    expected = (
        {core.lead.user_id for core in request.cores}
        | {member.user_id for core in request.cores for member in core.members}
        | {member.user_id for member in request.unplaced}
    )
    actual = [member.user_id for team in plan.teams for member in team.members] + [
        ranked.user_id for ranked in plan.reserve
    ]
    occurrences: dict[UUID, int] = {}
    for user_id in actual:
        occurrences[user_id] = occurrences.get(user_id, 0) + 1
    for user_id in sorted(expected - occurrences.keys(), key=str):
        errors.append(_violation(1, "Input person is missing from the plan", user_id=user_id))
    for user_id, count in occurrences.items():
        if user_id not in expected:
            errors.append(_violation(1, "Plan contains an unknown person", user_id=user_id))
        for _ in range(count - 1):
            errors.append(_violation(1, "Person appears more than once", user_id=user_id))

    core_ids = {core.project_id for core in request.cores}
    by_project: dict[UUID, list[int]] = {}
    for index, team in enumerate(plan.teams):
        if team.project_id is not None:
            by_project.setdefault(team.project_id, []).append(index)
    for core in request.cores:
        indexes = by_project.get(core.project_id, [])
        if not indexes:
            errors.append(_violation(2, "Core has no plan team", project_id=core.project_id))
            continue
        if len(indexes) > 1:
            errors.append(
                _violation(2, "Core appears in more than one team", project_id=core.project_id)
            )
        planned = {member.user_id for member in plan.teams[indexes[0]].members}
        for member in (core.lead, *core.members):
            if member.user_id not in planned:
                errors.append(
                    _violation(
                        2,
                        "Core member is not on the core team",
                        user_id=member.user_id,
                        project_id=core.project_id,
                    )
                )
    for project_id in by_project.keys() - core_ids:
        errors.append(_violation(2, "Team references an unknown project", project_id=project_id))
    for team in plan.teams:
        if team.project_id in core_ids and team.lead_user_id is not None:
            errors.append(
                _violation(
                    2,
                    "Core team must not set a planned lead",
                    user_id=team.lead_user_id,
                    project_id=team.project_id,
                )
            )

    for team in plan.teams:
        if len(team.members) > request.max_size:
            errors.append(
                _violation(3, "Team exceeds the maximum size", project_id=team.project_id)
            )
        if team.project_id is None and len(team.members) < request.viable:
            errors.append(_violation(3, "New team is below the viable size"))
    undersized = [
        team
        for team in plan.teams
        if team.project_id in core_ids and len(team.members) < request.viable
    ]
    if len(undersized) > request.bounds.undersized_cores:
        errors.append(_violation(3, "Too many undersized cores"))
    sizes = {
        project_id: len(plan.teams[indexes[0]].members)
        for project_id, indexes in by_project.items()
    }
    merged_ids = {
        project_id
        for merge in plan.merges
        if merge.absorbed_team_id != merge.target_team_id
        and merge.absorbed_team_id in sizes
        and merge.target_team_id in sizes
        and sizes[merge.absorbed_team_id] + sizes[merge.target_team_id] <= request.max_size
        for project_id in (merge.absorbed_team_id, merge.target_team_id)
    }
    for core_team in undersized:
        if core_team.project_id in merged_ids:
            continue
        size = len(core_team.members)
        fitting = any(
            other is not core_team
            and other.project_id is not None
            and other.project_id not in merged_ids
            and len(other.members) + size <= request.max_size
            for other in plan.teams
        )
        if fitting:
            errors.append(
                _violation(
                    3, "Undersized core has no merge proposal", project_id=core_team.project_id
                )
            )

    bounds = request.bounds
    if len(plan.teams) != bounds.teams:
        errors.append(_violation(4, "Team count differs from the bounds"))
    if sum(1 for team in plan.teams if team.project_id is None) != bounds.new_teams:
        errors.append(_violation(4, "New team count differs from the bounds"))
    placed = sum(len(team.members) for team in plan.teams)
    free_placed = placed - sum(1 + len(core.members) for core in request.cores)
    if free_placed != bounds.placed:
        errors.append(_violation(4, "Placed count differs from the bounds"))
    if len(plan.reserve) != bounds.reserve:
        errors.append(_violation(4, "Reserve count differs from the bounds"))
    if sorted(ranked.rank for ranked in plan.reserve) != list(range(1, len(plan.reserve) + 1)):
        errors.append(_violation(4, "Reserve ranks are not 1..n"))

    facts = formation_member_facts(request, settings)
    for team in plan.teams:
        if team.project_id is not None:
            continue
        member_ids = [member.user_id for member in team.members]
        if team.lead_user_id is None:
            errors.append(_violation(5, "New team has no planned lead"))
            continue
        if team.lead_user_id not in member_ids:
            errors.append(
                _violation(5, "Planned lead is not on the team", user_id=team.lead_user_id)
            )
            continue
        known = [user_id for user_id in member_ids if user_id in facts]
        if team.lead_user_id != planned_lead_user(facts, known):
            errors.append(
                _violation(
                    5, "Planned lead does not follow the lead order", user_id=team.lead_user_id
                )
            )

    seen: set[tuple[UUID, UUID]] = set()
    for merge in plan.merges:
        absorbed, target = merge.absorbed_team_id, merge.target_team_id
        if absorbed not in sizes or target not in sizes:
            errors.append(
                _violation(6, "Merge references a team outside the plan", project_id=absorbed)
            )
        elif absorbed == target:
            errors.append(_violation(6, "Merge targets the same team", project_id=absorbed))
        elif sizes[absorbed] + sizes[target] > request.max_size:
            errors.append(
                _violation(6, "Merged team exceeds the maximum size", project_id=absorbed)
            )
        elif (absorbed, target) in seen:
            errors.append(_violation(6, "Merge pair repeats", project_id=absorbed))
        seen.add((absorbed, target))
    return errors


AREA_IMPORTANCE_RANK: dict[AreaImportance, int] = {
    AreaImportance.PRIMARY: 0,
    AreaImportance.REQUIRED: 1,
    AreaImportance.NICE: 2,
}


def area_priority(request: FormationRequest) -> dict[AreaKey, int]:
    priority: dict[AreaKey, int] = {}
    for area in sorted(
        request.hackathon.areas,
        key=lambda area: (AREA_IMPORTANCE_RANK[area.importance], area.area),
    ):
        priority.setdefault(area.area, AREA_IMPORTANCE_RANK[area.importance])
    return priority


def area_score(member: MemberSnapshot, key: AreaKey) -> float:
    score = member.area_scores.get(key)
    return score.effective if score is not None else 0.0


def best_area(request: FormationRequest, member: EnrolledMember) -> AreaKey | None:
    if member.preferred_area is not None:
        return member.preferred_area
    priority = area_priority(request)
    if not priority:
        return None
    return min(priority, key=lambda key: (-area_score(member, key), priority[key], key))


@dataclass(slots=True)
class PlanDraft:
    project_id: UUID | None
    members: list[PlannedMember]
    lead_user_id: UUID | None = None


def fallback_plan(request: FormationRequest, settings: AlgoSettings) -> FormationPlan:
    viable, max_size, bounds = request.viable, request.max_size, request.bounds
    priority = area_priority(request)
    facts = formation_member_facts(request, settings)
    cores = [
        PlanDraft(
            project_id=core.project_id,
            members=[
                PlannedMember(
                    user_id=member.user_id, area=member.area, reason="Preserved core member"
                )
                for member in (core.lead, *core.members)
            ],
        )
        for core in request.cores
    ]
    pool = sorted(request.unplaced, key=lambda member: (member.enrolled_at, str(member.user_id)))
    placed = 0

    def take(draft: PlanDraft, member: EnrolledMember, area: AreaKey | None, reason: str) -> None:
        nonlocal placed
        pool.remove(member)
        draft.members.append(PlannedMember(user_id=member.user_id, area=area, reason=reason))
        placed += 1

    deficits = sorted(
        (viable - len(draft.members), index)
        for index, draft in enumerate(cores)
        if len(draft.members) < viable
    )
    for deficit, index in deficits:
        draft = cores[index]
        for _ in range(deficit):
            if not pool or placed >= bounds.placed:
                break
            covered = {member.area for member in draft.members if member.area is not None}
            uncovered = [key for key in priority if key not in covered]
            if uncovered:
                key = uncovered[0]
                chosen = min(
                    pool,
                    key=lambda member: (
                        -area_score(member, key),
                        member.enrolled_at,
                        str(member.user_id),
                    ),
                )
                take(draft, chosen, key, "Fills a core toward the viable size")
            else:
                chosen = pool[0]
                take(
                    draft,
                    chosen,
                    best_area(request, chosen),
                    "Fills a core toward the viable size",
                )
        if not pool or placed >= bounds.placed:
            break

    new_teams: list[PlanDraft] = []
    for _ in range(bounds.new_teams):
        draft = PlanDraft(project_id=None, members=[])
        new_teams.append(draft)
        if not pool:
            continue
        volunteer = planned_lead_user(
            facts, [member.user_id for member in pool if member.willing_to_lead]
        )
        chosen = next(
            (member for member in pool if member.user_id == volunteer),
            pool[0],
        )
        take(draft, chosen, best_area(request, chosen), "Seeds a new team")

    while pool and placed < bounds.placed:
        short = [
            (len(draft.members), index, draft)
            for index, draft in enumerate(new_teams)
            if len(draft.members) < viable
        ]
        if not short:
            break
        draft = min(short, key=lambda entry: (entry[0], entry[1]))[2]
        chosen = pool[0]
        take(draft, chosen, best_area(request, chosen), "Fills a bounded team seat")

    ranked_drafts = [(0, index, draft) for index, draft in enumerate(new_teams)] + [
        (1, index, draft) for index, draft in enumerate(cores)
    ]
    while pool and placed < bounds.placed:
        open_drafts = [entry for entry in ranked_drafts if len(entry[2].members) < max_size]
        if not open_drafts:
            break
        draft = min(open_drafts, key=lambda entry: (len(entry[2].members), entry[0], entry[1]))[2]
        chosen = pool[0]
        take(draft, chosen, best_area(request, chosen), "Balances team sizes")

    for draft in new_teams:
        draft.lead_user_id = planned_lead_user(facts, [member.user_id for member in draft.members])

    reserve = [
        RankedMember(
            user_id=member.user_id,
            rank=rank,
            reason="Kept on the reserve by the fallback planner",
        )
        for rank, member in enumerate(pool, start=1)
    ]

    merges: list[PlannedMerge] = []
    used: set[UUID] = set()
    undersized = sorted(
        (len(draft.members), index)
        for index, draft in enumerate(cores)
        if len(draft.members) < viable
    )
    for _, index in undersized:
        draft = cores[index]
        if draft.project_id is None or draft.project_id in used:
            continue
        target = next(
            (
                other
                for other in cores
                if other is not draft
                and other.project_id is not None
                and other.project_id not in used
                and len(other.members) + len(draft.members) <= max_size
            ),
            None,
        )
        if target is None or target.project_id is None:
            continue
        merges.append(
            PlannedMerge(
                absorbed_team_id=draft.project_id,
                target_team_id=target.project_id,
                reason="Undersized core fits into another team without exceeding the maximum",
            )
        )
        used.update((draft.project_id, target.project_id))

    return FormationPlan(
        teams=[
            PlannedTeam(
                project_id=draft.project_id,
                lead_user_id=draft.lead_user_id,
                members=draft.members,
                reason="Preserved core" if draft.project_id is not None else "New team",
            )
            for draft in [*cores, *new_teams]
        ],
        reserve=reserve,
        merges=merges,
        summary=(
            "Deterministic fallback: cores filled to viable, volunteers seeded first, "
            "then area coverage and balanced sizes"
        ),
    )
