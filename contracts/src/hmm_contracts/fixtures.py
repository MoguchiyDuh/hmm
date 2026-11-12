from pathlib import Path
from typing import Literal

from pydantic import Field, field_serializer

from hmm_contracts.algo import (
    CommunityGraph,
    EventSnapshot,
    FormationRequest,
    MemberHistory,
    MemberSnapshot,
    MentorMatchRequest,
    MentorshipEdge,
)
from hmm_contracts.common import (
    AreaKey,
    Contract,
    MentorshipKind,
    OnboardingStatus,
    ProjectId,
    ProjectVisibility,
    RestrictionKind,
    SubjectId,
    UserId,
)
from hmm_contracts.imports import EventImport
from hmm_contracts.notifications import Notification

__all__ = [
    "CommunityCase",
    "FormationCase",
    "FormationExpect",
    "MentorExclusion",
    "MentorshipCase",
    "MentorshipExpect",
    "PairClassification",
    "PardonedPair",
    "RankExpectation",
    "VisibilityCase",
    "VisibilitySubject",
    "VisibilityViewer",
    "fixtures_dir",
    "load_community_cases",
    "load_events",
    "load_formation_cases",
    "load_imports",
    "load_members",
    "load_mentorship_cases",
    "load_notifications",
    "load_visibility_cases",
]


class FormationExpect(Contract):
    primary_coverage_per_team: dict[AreaKey, int] = Field(default_factory=dict)
    required_coverage_per_team: dict[AreaKey, int] = Field(default_factory=dict)
    fixed_cores: dict[ProjectId, list[UserId]] = Field(default_factory=dict)
    new_teams_with_volunteers: int = Field(ge=0)
    new_team_lead_tiers: list[int] = Field(default_factory=list)
    team_sizes: list[int]
    merge_proposal_required: bool = False
    pair_beginners_with_specialists: bool = True


class FormationCase(Contract):
    description: str = Field(min_length=1)
    request: FormationRequest
    expect: FormationExpect


class RankExpectation(Contract):
    user_id: UserId
    top_n: int = Field(ge=1)


class PairClassification(Contract):
    mentor_id: UserId
    learner_id: UserId
    kind: MentorshipKind


class MentorExclusion(Contract):
    member: MemberSnapshot
    reason: Literal["capacity_full", "restricted", "dnd", "banned"]
    restrictions: list[RestrictionKind] = Field(default_factory=list)


class MentorshipExpect(Contract):
    ranked: list[RankExpectation]
    must_not_appear: list[UserId]
    classifications: list[PairClassification]


class MentorshipCase(Contract):
    description: str = Field(min_length=1)
    request: MentorMatchRequest
    expect: MentorshipExpect
    excluded_candidates: list[MentorExclusion] = Field(default_factory=list)
    pair_history: list[MentorshipEdge] = Field(default_factory=list)


class PardonedPair(Contract):
    user_ids: tuple[UserId, UserId]
    subject_id: SubjectId
    reason: str = Field(min_length=1)


class CommunityCase(Contract):
    description: str = Field(min_length=1)
    graph: CommunityGraph
    expect_flagged: list[set[UserId]]
    expect_not_flagged: list[UserId]
    pardoned_pairs: list[PardonedPair] = Field(default_factory=list)

    @field_serializer("expect_flagged")
    def serialize_flagged(self, value: list[set[UserId]]) -> list[list[str]]:
        return [sorted(str(user_id) for user_id in ring) for ring in value]


class VisibilityViewer(Contract):
    role: Literal[
        "anonymous",
        "member",
        "admin",
        "lead",
        "team_member",
        "learner",
        "target_mentor",
        "owner",
        "reviewer",
        "reviewee",
        "banned",
        "onboarding",
    ]
    user_id: UserId | None = None
    project_id: ProjectId | None = None
    onboarding_status: OnboardingStatus = OnboardingStatus.COMPLETED
    banned: bool = False


class VisibilitySubject(Contract):
    kind: Literal[
        "profile",
        "cv",
        "mentor_offers",
        "mentor_matching",
        "quick_pick",
        "formation",
        "retrospective",
        "review",
        "project",
        "direct_help_request",
        "pending_invites",
        "pending_requests",
        "pending_offers",
        "onboarding",
        "notifications",
        "own_profile",
        "facts",
    ]
    id: SubjectId
    owner_id: UserId | None = None
    project_id: ProjectId | None = None
    learner_id: UserId | None = None
    target_mentor_id: UserId | None = None
    visibility: ProjectVisibility | None = None
    dnd: bool = False
    review_window_closed: bool | None = None
    to_check: bool = False
    excluded: bool = False
    pending: bool = False


class VisibilityCase(Contract):
    description: str = Field(min_length=1)
    operation: Literal["read", "edit", "upload", "match", "place"] = "read"
    viewer: VisibilityViewer
    subject: VisibilitySubject
    expect_visible: bool
    visible_fields: list[str] | None = None


def fixtures_dir() -> Path:
    directory = Path(__file__).resolve().parents[2] / "fixtures"
    if not directory.is_dir():
        raise FileNotFoundError(
            f"Shared contracts fixtures not found at {directory}; "
            "use an editable repository install"
        )
    return directory


def _load[T: Contract](directory: str, model: type[T]) -> dict[str, T]:
    return {
        path.stem: model.model_validate_json(path.read_bytes())
        for path in sorted((fixtures_dir() / directory).glob("*.json"))
    }


def load_members() -> dict[str, MemberHistory]:
    return _load("members", MemberHistory)


def load_events() -> dict[str, EventSnapshot]:
    return _load("events", EventSnapshot)


def load_formation_cases() -> dict[str, FormationCase]:
    return _load("formation", FormationCase)


def load_mentorship_cases() -> dict[str, MentorshipCase]:
    return _load("mentorship", MentorshipCase)


def load_community_cases() -> dict[str, CommunityCase]:
    return _load("community", CommunityCase)


def load_visibility_cases() -> dict[str, VisibilityCase]:
    return _load("visibility", VisibilityCase)


def load_imports() -> dict[str, EventImport]:
    return _load("imports", EventImport)


def load_notifications() -> dict[str, Notification]:
    return _load("notifications", Notification)
