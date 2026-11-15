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
from hmm_contracts.formation import Unit, pack

__all__ = ["FakeAlgorithm"]


def _score(member: a.MemberSnapshot, area: AreaKey) -> float:
    score = member.area_scores.get(area)
    return score.effective if score is not None else 0.0


def _best_area(member: a.MemberSnapshot, areas: Iterable[AreaKey]) -> AreaKey | None:
    return min(areas, key=lambda area: (-_score(member, area), area), default=None)


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
        packing = pack(
            [
                Unit(
                    id=unit.id,
                    size=len(unit.members),
                    queued_at=min(member.enrolled_at for member in unit.members),
                )
                for unit in request.units
            ],
            [],
            request.target,
            request.min_size,
            request.max_size,
        )
        return a.FormationPlan(
            teams=[
                a.PlannedTeam(units=units, reason="Queue packing") for units in packing.new_teams
            ],
            summary="Pack whole units into balanced teams",
        )

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
