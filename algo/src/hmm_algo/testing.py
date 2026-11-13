from collections import defaultdict
from collections.abc import Iterable
from datetime import timedelta

from hmm_contracts import algo as a
from hmm_contracts.common import (
    AreaKey,
    FlagStatus,
    FlagSubjectType,
    Severity,
    SubjectId,
    Trust,
    UserId,
)
from hmm_contracts.formation import placement_bounds

__all__ = ["FakeAlgorithm"]


def _score(member: a.MemberSnapshot, area: AreaKey) -> float:
    score = member.area_scores.get(area)
    return score.effective if score is not None else 0.0


def _best_area(member: a.MemberSnapshot, areas: Iterable[AreaKey]) -> AreaKey | None:
    return min(areas, key=lambda area: (-_score(member, area), area), default=None)


def _rank(members: Iterable[a.EnrolledMember]) -> list[a.RankedMember]:
    ordered = sorted(members, key=lambda member: (member.enrolled_at, member.user_id))
    return [
        a.RankedMember(user_id=member.user_id, rank=index, reason="Earlier enrollment first")
        for index, member in enumerate(ordered, 1)
    ]


def _lead_order(
    ctx: a.AlgoContext, request: a.FormationRequest, member: a.EnrolledMember
) -> tuple[int, float, UserId]:
    fit = request.lead_fits.get(member.user_id)
    value = fit.fit if fit is not None else 0.0
    tier = 4
    if member.willing_to_lead:
        tier = 2 if value >= ctx.settings.lead_fit_threshold else 3
    return tier, -value, member.user_id


def _components(edges: list[tuple[UserId, UserId]]) -> list[set[UserId]]:
    neighbors: dict[UserId, set[UserId]] = defaultdict(set)
    for source, target in edges:
        neighbors[source].add(target)
        neighbors[target].add(source)
    unseen = set(neighbors)
    result: list[set[UserId]] = []
    while unseen:
        pending = [min(unseen)]
        component: set[UserId] = set()
        while pending:
            user_id = pending.pop()
            if user_id in component:
                continue
            component.add(user_id)
            pending.extend(neighbors[user_id] - component)
        unseen -= component
        result.append(component)
    return result


class FakeAlgorithm:
    version: str = "fake-1"

    async def score_onboarding(
        self, ctx: a.AlgoContext, request: a.OnboardingRequest
    ) -> a.OnboardingScores:
        hints: dict[AreaKey, list[float]] = defaultdict(list)
        for question in request.quiz.questions:
            answer = request.answers.answers.get(question.key)
            if answer is None:
                continue
            values = answer.value if isinstance(answer.value, list) else [str(answer.value)]
            for option in question.options:
                if option.key in values:
                    for area, value in option.area_hints.items():
                        hints[area].append(value * 10)
        scores: dict[AreaKey, a.ScoreWithReason] = {}
        discrepancies: list[str] = []
        for area in request.areas:
            quiz = max(hints[area.key], default=0.0)
            text = (request.cv_text or "").casefold()
            cv = 3.0 if area.name.casefold() in text or area.key in text else 0.0
            github = 0.0
            if request.github is not None:
                matching = sum(
                    area.key in repo.topics
                    or area.name.casefold() in (repo.description or "").casefold()
                    for repo in request.github.repos
                )
                github = min(6.0, matching * 2.0)
            if quiz >= 8 and max(cv, github) == 0:
                discrepancies.append(f"High quiz score in {area.key} without CV or GitHub evidence")
            scores[area.key] = a.ScoreWithReason(
                value=min(10.0, max(quiz, cv, github)),
                reason="Maximum of quiz hints, CV area mentions and matching GitHub repositories",
            )
        return a.OnboardingScores(area_scores=scores, discrepancies=discrepancies)

    async def compute_member(
        self, ctx: a.AlgoContext, member: a.MemberHistory, areas: list[a.Area]
    ) -> a.MemberMetrics:
        awards: list[a.FactAward] = []
        progress: dict[AreaKey, float] = defaultdict(float)
        for fact in member.facts:
            blocked = fact.excluded or fact.flag_status == FlagStatus.TO_CHECK
            result = fact.context.result
            absent = result is not None and not result.participants.get(member.user_id, False)
            xp = (
                0.0
                if blocked or absent
                else {Trust.SELF: 0.0, Trust.PEER: 5.0, Trust.TRUSTED: 10.0}[fact.trust]
            )
            recent = (
                ctx.now - timedelta(days=ctx.settings.activity_window_days)
                <= (fact.ended_at or fact.started_at)
                <= ctx.now
            )
            activity = 1.0 if recent and not blocked and not absent else 0.0
            awards.append(
                a.FactAward(
                    fact_type=fact.type,
                    fact_id=fact.id,
                    xp=xp,
                    activity=activity,
                    reason="Trust-based award; blocked facts and absent participation earn zero",
                )
            )
            if fact.area is not None:
                progress[fact.area] += xp / 100
        scores: dict[AreaKey, a.ScoreWithReason] = {}
        for area in areas:
            baseline = (
                member.onboarding.area_scores.get(area.key)
                if member.onboarding is not None
                else None
            )
            scores[area.key] = a.ScoreWithReason(
                value=min(
                    10.0, (baseline.value if baseline is not None else 0.0) + progress[area.key]
                ),
                reason="Onboarding baseline plus fact progress, without effective overrides",
            )
        return a.MemberMetrics(
            area_scores=scores,
            xp=sum(award.xp for award in awards),
            activity=sum(award.activity for award in awards),
            fact_awards=awards,
            flags=[],
        )

    async def lead_fit(
        self, ctx: a.AlgoContext, hackathon: a.EventSnapshot, candidates: list[a.MemberSnapshot]
    ) -> list[a.LeadFit]:
        areas = [area.area for area in hackathon.areas if area.importance == "primary"]
        if not areas:
            areas = [area.area for area in hackathon.areas]
        result: list[a.LeadFit] = []
        for member in sorted(candidates, key=lambda member: member.user_id):
            strength = max((_score(member, area) for area in areas), default=0.0) / 10
            experience = min(1.0, member.hackathons_finished / 5)
            rating = member.reputation.get("lead")
            reputation = rating.avg / 10 if rating is not None and rating.avg is not None else 0.0
            fit = 0.6 * strength + 0.25 * experience + 0.15 * reputation
            if strength <= 0.3:
                fit = min(fit, 0.59)
            result.append(
                a.LeadFit(
                    user_id=member.user_id,
                    fit=fit,
                    reason="Effective event-area strength, finished hackathons and lead reputation",
                )
            )
        return result

    async def recommend(self, ctx: a.AlgoContext, request: a.RecommendRequest) -> a.Recommendations:
        areas = (
            request.project.recruiting.areas
            if request.project.recruiting
            else request.project.areas
        )

        def rank(members: Iterable[a.MemberSnapshot], limit: int) -> list[a.Rec]:
            recs: list[a.Rec] = []
            for member in members:
                area = _best_area(member, areas)
                value = _score(member, area) / 10 if area is not None else 0.0
                recs.append(
                    a.Rec(
                        user_id=member.user_id,
                        score=value,
                        reasons=["Effective strength in the project's requested areas"],
                        tags=[],
                        suggested_area=area,
                    )
                )
            return sorted(recs, key=lambda rec: (-rec.score, rec.user_id))[:limit]

        return a.Recommendations(
            enrolled=rank(request.enrolled, request.limit_enrolled),
            others=rank(request.others, request.limit_others),
        )

    async def form_teams(self, ctx: a.AlgoContext, request: a.FormationRequest) -> a.FormationPlan:
        bounds = placement_bounds(
            len(request.unplaced),
            [1 + len(core.members) for core in request.cores],
            request.viable,
            request.target,
            request.max_size,
        )
        if request.bounds != bounds:
            raise ValueError("Formation bounds do not match the given candidates and core sizes")
        teams = [
            a.PlannedTeam(
                project_id=core.project_id,
                lead_user_id=None,
                members=[
                    a.PlannedMember(
                        user_id=member.user_id,
                        area=member.area,
                        reason="Preserves the claimed core",
                    )
                    for member in [core.lead, *core.members]
                ],
                reason="Claimed core remains intact",
            )
            for core in request.cores
        ]
        remaining = sorted(request.unplaced, key=lambda member: _lead_order(ctx, request, member))
        placed = 0

        def add(team: a.PlannedTeam) -> None:
            nonlocal placed
            member = remaining.pop(0)
            area = member.preferred_area or _best_area(
                member, [area.area for area in request.hackathon.areas]
            )
            team.members.append(
                a.PlannedMember(
                    user_id=member.user_id, area=area, reason="Fills a bounded team seat"
                )
            )
            placed += 1

        for team in sorted(teams, key=lambda team: (-len(team.members), team.project_id)):
            while len(team.members) < request.viable and placed < bounds.placed:
                add(team)
        for _ in range(bounds.new_teams):
            team = a.PlannedTeam(
                project_id=None,
                lead_user_id=remaining[0].user_id,
                members=[],
                reason="New viable team",
            )
            add(team)
            teams.append(team)
        for team in teams[len(request.cores) :]:
            while len(team.members) < request.viable:
                add(team)
        while placed < bounds.placed:
            team = min(
                (team for team in teams if len(team.members) < request.max_size),
                key=lambda team: len(team.members),
            )
            add(team)
        merges: list[a.PlannedMerge] = []
        for team in teams:
            if len(team.members) >= request.viable or team.project_id is None:
                continue
            target = next(
                (
                    other
                    for other in teams
                    if other is not team
                    and other.project_id is not None
                    and len(other.members) + len(team.members) <= request.max_size
                ),
                None,
            )
            if target is not None and target.project_id is not None:
                merges.append(
                    a.PlannedMerge(
                        absorbed_team_id=team.project_id,
                        target_team_id=target.project_id,
                        reason="Undersized cores can fit together without exceeding the maximum",
                    )
                )
        return a.FormationPlan(
            teams=teams,
            reserve=_rank(remaining),
            merges=merges,
            summary="Fill closest cores first, seed leads by tier, then balance bounded team sizes",
        )

    async def rank_reserve(
        self, ctx: a.AlgoContext, request: a.ReserveRequest
    ) -> list[a.RankedMember]:
        return _rank(request.reserve)

    async def match_mentors(self, ctx: a.AlgoContext, request: a.MentorMatchRequest) -> list[a.Rec]:
        result: list[a.Rec] = []
        for member in request.candidates:
            if (
                member.user_id == request.learner.user_id
                or member.mentor_active >= member.mentor_capacity
            ):
                continue
            strength = _score(member, request.area.key)
            gap = strength - _score(request.learner, request.area.key)
            kind = (
                "mentoring"
                if strength >= ctx.settings.mentor_min_score and gap >= ctx.settings.mentor_min_gap
                else "study_pair"
            )
            result.append(
                a.Rec(
                    user_id=member.user_id,
                    score=strength / 10,
                    reasons=["Effective area strength with free mentoring capacity"],
                    tags=[kind],
                    suggested_area=request.area.key,
                )
            )
        return sorted(result, key=lambda rec: (-rec.score, rec.user_id))[: request.limit]

    async def scan_community(self, ctx: a.AlgoContext, graph: a.CommunityGraph) -> list[a.Flag]:
        flags: list[a.Flag] = []
        pardoned = {(flag.subject_type, flag.subject_id, flag.reason) for flag in graph.pardoned}

        def flag(
            kind: FlagSubjectType, subject: SubjectId, users: set[UserId], reason: str
        ) -> None:
            if (kind, subject, reason) not in pardoned:
                flags.append(
                    a.Flag(
                        subject_type=kind,
                        subject_id=subject,
                        related_user_ids=sorted(users),
                        reason=reason,
                        severity=Severity.MEDIUM,
                    )
                )

        confirmations = [
            edge
            for edge in graph.confirmations
            if edge.confirmer_id is not None
            and not edge.ignored
            and edge.withdrawn_at is None
            and edge.flag_status != FlagStatus.PARDONED
        ]
        pairs = [
            (edge.confirmer_id, edge.subject_user_id)
            for edge in confirmations
            if edge.confirmer_id is not None
        ]
        for users in _components(pairs):
            edges = [edge for edge in confirmations if edge.subject_user_id in users]
            if len({edge.context_id for edge in edges}) < 3:
                continue
            if {edge.confirmer_id for edge in edges} != users:
                continue
            for edge in edges:
                flag(
                    FlagSubjectType.CONFIRMATION,
                    edge.id,
                    users,
                    "Repeated confirmations within a closed account cluster",
                )
        mentorships: dict[tuple[UserId, UserId], list[a.MentorshipEdge]] = defaultdict(list)
        for edge in graph.mentorships:
            pair = (min(edge.mentor_id, edge.learner_id), max(edge.mentor_id, edge.learner_id))
            mentorships[pair].append(edge)
        for pair, edges in sorted(mentorships.items()):
            if len(edges) >= 4 and len({edge.mentor_id for edge in edges}) == 2:
                for edge in edges:
                    flag(
                        FlagSubjectType.MENTORSHIP,
                        edge.id,
                        set(pair),
                        "Repeated reciprocal mentorships between the same accounts",
                    )
        reviews = [
            edge
            for edge in graph.reviews
            if edge.rating == 10
            and edge.removed_at is None
            and edge.flag_status != FlagStatus.PARDONED
        ]
        for users in _components([(edge.reviewer_id, edge.reviewee_id) for edge in reviews]):
            edges = [edge for edge in reviews if edge.reviewer_id in users]
            if (
                len({edge.subject_id for edge in edges}) >= 3
                and {edge.reviewee_id for edge in edges} == users
            ):
                for edge in edges:
                    flag(
                        FlagSubjectType.REVIEW,
                        edge.id,
                        users,
                        "Repeated perfect ratings within a closed account cluster",
                    )
        return flags
