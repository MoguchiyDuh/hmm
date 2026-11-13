import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from hmm_contracts.algo import (
    Area,
    CommunityGraph,
    Core,
    EnrolledMember,
    EventSnapshot,
    Fact,
    FormationRequest,
    LeadFit,
    MemberHistory,
    MemberSnapshot,
    MentorMatchRequest,
    MentorshipEdge,
    TeamMemberSnapshot,
)
from hmm_contracts.common import SEED_AREA_KEYS, Contract
from hmm_contracts.fixtures import (
    CommunityCase,
    FormationCase,
    MentorshipCase,
    VisibilityCase,
)
from hmm_contracts.formation import placement_bounds
from hmm_contracts.notifications import (
    ALLOWED_ACTIONS,
    ActionKey,
    ApiAction,
    LinkAction,
    Notification,
    NotificationType,
)

type Payload = dict[str, object]

NOW = datetime(2026, 11, 12, 10, tzinfo=UTC)
NAMES = (
    "Ada Morozova",
    "Boris Chen",
    "Clara Singh",
    "Daniil Park",
    "Elena Ortiz",
    "Farid Ali",
    "Greta Kim",
    "Hugo Santos",
    "Iris Novak",
    "Jamal Reed",
    "Kira Sato",
    "Lev Ivanov",
    "Mina Patel",
    "Noah Weber",
    "Olga Petrov",
    "Pavel Costa",
    "Quinn Brooks",
    "Rina Tan",
    "Samir Gupta",
    "Tara Hassan",
    "Uma Fischer",
    "Viktor Lee",
    "Wen Zhou",
    "Yara Ahmed",
    "Zoya Rossi",
    "Artem Silva",
    "Bea Laurent",
    "Cem Yilmaz",
)
PRIMARY = {
    0: "ml_ai",
    1: "frontend",
    2: "backend",
    3: "design",
    4: "ml_ai",
    5: "ml_ai",
    6: "backend",
    7: "frontend",
    8: "ml_ai",
    9: "backend",
    10: "frontend",
    11: "ml_ai",
    12: "data",
    13: "product",
    14: "ml_ai",
    15: "ml_ai",
    16: "backend",
    17: "frontend",
    18: "mobile",
    19: "hardware",
    20: "security",
    21: "qa",
    22: "ml_ai",
    23: "ml_ai",
    24: "ml_ai",
    25: "ml_ai",
    26: "ml_ai",
    27: "backend",
}
LEVELS = {
    0: 8.8,
    1: 9.0,
    2: 8.5,
    3: 2.5,
    4: 8.6,
    5: 2.4,
    6: 2.8,
    7: 3.0,
    8: 7.5,
    9: 8.2,
    10: 8.1,
    11: 3.0,
    12: 8.4,
    13: 3.5,
    14: 9.0,
    15: 9.2,
    16: 8.0,
    17: 5.0,
    18: 4.0,
    19: 6.0,
    20: 8.0,
    21: 5.0,
    22: 5.0,
    23: 8.6,
    24: 7.8,
    25: 7.0,
    26: 8.0,
    27: 8.0,
}


def uid(index: int) -> UUID:
    return UUID(f"019a71c0-0000-7000-8000-{index:012x}")


def user(index: int) -> UUID:
    return uid(index + 1)


def area_key(index: int) -> str:
    return PRIMARY.get(index, SEED_AREA_KEYS[index % len(SEED_AREA_KEYS)])


def events() -> dict[str, EventSnapshot]:
    definitions = [
        ("ml_hackathon", "Applied ML for Communities", "hackathon", 3, 6, "ml_ai"),
        ("small_hackathon", "Open Tools Sprint", "hackathon", 2, 4, "backend"),
        ("hardware_hackathon", "Repairable Devices Jam", "hackathon", 2, 5, "hardware"),
        ("security_contest", "Community Capture the Flag", "contest", 1, 3, "security"),
        ("data_meetup", "Data Quality Clinic", "meetup", None, None, "data"),
        ("design_meetup", "Accessible Interfaces Evening", "meetup", None, None, "design"),
        ("ml_workshop", "Model Evaluation Workshop", "workshop", None, None, "ml_ai"),
        ("member_meetup", "Local Rust Study Evening", "meetup", None, None, "backend"),
    ]
    output: dict[str, EventSnapshot] = {}
    for index, (stem, title, kind, minimum, maximum, primary) in enumerate(definitions):
        areas: list[Payload] = [{"area": primary, "importance": "primary", "headcount": 1}]
        if kind == "hackathon":
            areas.extend(
                {"area": key, "importance": importance, "headcount": 1}
                for key, importance in [
                    ("backend", "required"),
                    ("frontend", "required"),
                    ("design", "nice"),
                ]
                if key != primary
            )
        output[stem] = EventSnapshot.model_validate(
            {
                "id": uid(1000 + index),
                "title": title,
                "summary": f"Practical, shared outcomes focused on {primary}.",
                "description_md": "Build, demonstrate and discuss reproducible work with peers.",
                "themes": [primary, "open collaboration"],
                "format": ["online", "hybrid", "offline"][index % 3],
                "starts_at": NOW + timedelta(days=14 + index),
                "ends_at": NOW + timedelta(days=14 + index, hours=36 if maximum else 3),
                "timezone": "UTC",
                "areas": areas,
                "team_size_min": minimum,
                "team_size_max": maximum,
                "difficulty": 7.0 if maximum else 4.0,
                "kind": kind,
                "origin": "member" if index == 7 else "import",
                "created_by": user(19) if index == 7 else None,
            }
        )
    return output


def confirmation(index: int, source: str, ignored: bool = False) -> Payload:
    return {
        "id": uid(20000 + index),
        "confirmer_id": None if source == "system" else user(16 if ignored else (index + 2) % 120),
        "source": source,
        "confirmer_account_age": None
        if source == "system"
        else timedelta(days=3 if ignored else 500),
        "confirmer_xp": None if source == "system" else 5.0 if ignored else 400.0,
        "confirmer_restrictions": ["confirming"] if ignored else [],
        "ignored": ignored,
        "flag_status": None,
        "created_at": NOW - timedelta(days=8),
        "withdrawn_at": None,
    }


def project(index: int, event: EventSnapshot | None = None) -> Payload:
    return {
        "id": uid(3000 + index),
        "name": f"Community toolkit {index + 1}",
        "goal": "Ship a reproducible tool that solves a documented community problem",
        "description_md": "Small, maintained tooling with shared ownership and a public release.",
        "areas": [area_key(index)],
        "recruiting": None,
        "event_id": event.id if event else None,
        "status": "shipped",
        "team_status": "finished" if event else None,
        "links": [f"https://github.com/hmm-fixtures/toolkit-{index + 1}"],
    }


def fact(index: int, **changes: object) -> Fact:
    payload: Payload = {
        "id": uid(10000 + index),
        "type": "attendance",
        "role": "attendee",
        "area": area_key(index % 120),
        "trust": "self",
        "flag_status": None,
        "excluded": False,
        "proof_links": [],
        "note": "Joined the practical discussion",
        "created_at": NOW - timedelta(days=11),
        "started_at": NOW - timedelta(days=10),
        "ended_at": NOW - timedelta(days=9),
        "confirmed_at": None,
        "left_at": None,
        "confirmations": [],
        "counterparts": [],
        "outcome": None,
        "effort": {"updates": [], "milestones": [], "checkins": [], "minutes": 120},
        "context": {
            "event": None,
            "project": None,
            "result": None,
            "releases": [],
            "mentorship": None,
            "attendance_status": "attended",
        },
    }
    payload.update(changes)
    return Fact.model_validate(payload)


def historical_event(event: EventSnapshot, days: int = 10) -> EventSnapshot:
    return EventSnapshot.model_validate(
        event.model_dump()
        | {
            "id": uid(120000 + (event.id.int & 0xFFFF) * 1000 + days),
            "starts_at": NOW - timedelta(days=days),
            "ends_at": NOW - timedelta(days=days - 1),
        }
    )


def hackathon_fact(index: int, event: EventSnapshot, sequence: int = 0) -> Fact:
    ghost = index == 17
    verified = index % 2 == 0
    participant = user(index)
    days = (400 if index == 14 else 10) + sequence * 30
    event = historical_event(event, days)
    if sequence:
        event = EventSnapshot.model_validate(
            event.model_dump()
            | {
                "title": f"Community ML Sprint {sequence}",
            }
        )
    project_index = index + sequence * 120
    confirmation_time = NOW - timedelta(days=days - 2)
    return fact(
        project_index,
        type="project_member",
        area=area_key(index),
        role="lead" if index == 0 or (index == 1 and sequence < 2) else "member",
        trust="self" if ghost else "trusted",
        counterparts=[user((index + 1) % 120)],
        confirmations=[]
        if ghost
        else [confirmation(project_index, "system") | {"created_at": confirmation_time}],
        created_at=NOW - timedelta(days=days + 1),
        started_at=event.starts_at,
        ended_at=event.ends_at,
        confirmed_at=None if ghost else confirmation_time,
        outcome="finished",
        note="Did not participate despite confirmation"
        if ghost
        else "Delivered the working prototype",
        context={
            "event": event,
            "project": project(project_index, event),
            "result": {
                "outcome": "finished",
                "place": 2,
                "award": "Community impact prize",
                "place_status": "verified" if verified else "unverified",
                "participants": {participant: not ghost},
                "member_areas": {participant: area_key(index)},
                "repo_url": f"https://github.com/hmm-fixtures/toolkit-{project_index + 1}",
                "demo_url": None,
                "presentation_url": None,
                "retrospective_md": "The baseline shipped; deployment took longer than planned.",
                "auto_closed": False,
                "submitted_at": confirmation_time,
            },
            "releases": [],
            "mentorship": None,
            "attendance_status": None,
        },
    )


def mentorship_fact(index: int, counterpart: int, mentor: bool, repeat: int = 0) -> Fact:
    mentor_id, learner_id = (
        (user(index), user(counterpart)) if mentor else (user(counterpart), user(index))
    )
    return fact(
        500 + index,
        type="mentorship_side",
        role="mentor" if mentor else "mentee",
        area="ml_ai",
        trust="trusted",
        outcome="reached",
        counterparts=[user(counterpart)],
        started_at=NOW - timedelta(days=10),
        ended_at=NOW - timedelta(days=2),
        confirmed_at=NOW - timedelta(days=2),
        confirmations=[
            confirmation(500 + index, "learner" if mentor else "mentor")
            | {"confirmer_id": user(counterpart), "created_at": NOW - timedelta(days=2)}
        ],
        effort={
            "updates": [],
            "milestones": [],
            "minutes": 180,
            "checkins": [
                {
                    "id": uid(7000 + index * 10 + day),
                    "author_id": user(index),
                    "note": note,
                    "occurred_on": (NOW - timedelta(days=9 - day * 3)).date(),
                    "minutes": 60,
                }
                for day, note in enumerate(
                    [
                        "Established a reproducible baseline",
                        "Reviewed held-out evaluation",
                        "Compared error slices and documented the final approach",
                    ]
                )
            ],
        },
        context={
            "event": None,
            "project": None,
            "result": None,
            "releases": [],
            "attendance_status": None,
            "mentorship": {
                "id": uid(6000 + min(index, counterpart)),
                "kind": "mentoring",
                "status": "closed",
                "mentor_id": mentor_id,
                "learner_id": learner_id,
                "area": "ml_ai",
                "mentor_score_at_start": 8.6,
                "learner_score_at_start": 1.4 if index in (4, 5) else 0.8,
                "learner_score_now": 2.4 if index in (4, 5) else 1.0,
                "started_at": NOW - timedelta(days=10),
                "closed_at": NOW - timedelta(days=2),
                "duration": timedelta(days=8),
                "mentor_checkin_count": 3,
                "learner_checkin_count": 3,
                "earlier_pair_mentorships": repeat,
                "outcome": "reached",
                "summary": "Learner shipped a held-out evaluation report without data leakage",
                "mentor_acknowledged_at": NOW - timedelta(days=1),
                "endorsement": "Can now compare baselines and explain validation choices",
            },
        },
    )


def members(catalog: dict[str, EventSnapshot]) -> dict[str, MemberHistory]:
    output: dict[str, MemberHistory] = {}
    for index in range(120):
        primary = area_key(index)
        level = LEVELS.get(index, 3.0 if index % 3 == 0 else 7.0 + index % 4 / 10)
        fresh = index in (33, 34)
        if fresh:
            level = 2.4
        beginner = level < 4
        inactive = index == 14
        scores: dict[str, Payload] = {}
        for key in SEED_AREA_KEYS:
            value = level if key == primary else 1.0 + (index + len(key)) % 4 / 2
            if key == "ml_ai" and index in (1, 12, 16):
                value = {1: 6.0, 12: 7.0, 16: 8.4}[index]
            override = 8.7 if index == 22 and key == "ml_ai" else None
            scores[key] = {
                "calculated": value,
                "override": override,
                "override_evidence": "interview" if override else None,
                "effective": override if override else value,
            }
        history: list[Fact] = []
        hackathon_count = (
            0 if index >= 18 else 1 if beginner or index == 17 else 10 if inactive else 4
        )
        if index < 18:
            history.extend(
                hackathon_fact(index, catalog["ml_hackathon"], sequence)
                for sequence in range(hackathon_count)
            )
        else:
            trusted = index == 20 or index % 5 == 0
            peer = index == 19 or index % 5 == 1
            source = "system" if trusted else "peer"
            history.append(
                fact(
                    index,
                    trust="trusted" if trusted else "peer" if peer else "self",
                    role="speaker" if trusted else "attendee",
                    confirmations=[confirmation(index, source)] if trusted or peer else [],
                    counterparts=[user((index + 1) % 120)] if trusted or peer else [],
                    confirmed_at=NOW - timedelta(days=8) if trusted or peer else None,
                    context={
                        "event": historical_event(catalog["ml_workshop"]),
                        "project": None,
                        "result": None,
                        "releases": [],
                        "mentorship": None,
                        "attendance_status": "attended",
                    },
                    flag_status="to_check" if index == 21 else None,
                )
            )
        if index == 19:
            history.append(
                fact(
                    300 + index,
                    confirmations=[confirmation(300 + index, "peer", True)],
                    context={
                        "event": historical_event(catalog["data_meetup"]),
                        "project": None,
                        "result": None,
                        "releases": [],
                        "mentorship": None,
                        "attendance_status": "attended",
                    },
                )
            )
        if index in (4, 5, 26, 27):
            history.append(
                mentorship_fact(
                    index,
                    {4: 5, 5: 4, 26: 27, 27: 26}[index],
                    index in (4, 26),
                    3 if index >= 26 else 0,
                )
            )
        if index in (0, 2, 18, 20):
            solo = index == 18
            history.append(
                fact(
                    700 + index,
                    type="project_member",
                    role="lead" if solo else "member",
                    trust="self" if solo else "trusted",
                    outcome="shipped",
                    confirmations=[] if solo else [confirmation(700 + index, "lead")],
                    counterparts=[] if solo else [user(0 if index != 0 else 2)],
                    context={
                        "event": None,
                        "project": project(index + 2000),
                        "result": None,
                        "mentorship": None,
                        "attendance_status": None,
                        "releases": [
                            {
                                "id": uid(8000 + index),
                                "title": "First usable release",
                                "notes_md": "Reproducible workflow and report",
                                "repo_url": (
                                    f"https://github.com/hmm-fixtures/toolkit-{index + 2001}"
                                ),
                                "demo_url": None,
                                "writeup_url": None,
                                "created_at": NOW - timedelta(days=9),
                                "contributions": [
                                    {"user_id": user(index), "contributed": True, "area": primary}
                                ],
                            }
                        ],
                    },
                )
            )
        entries: list[Payload] = []
        if index in (5, 11, 18, 21):
            entry: Payload = {
                "id": uid(9000 + index),
                "type": "learning",
                "title": "Reproduce a baseline and document the error analysis",
                "description_md": "Worked through the evaluation protocol on an open dataset.",
                "links": [],
                "area": primary,
                "occurred_on": (NOW - timedelta(days=3)).date(),
                "created_at": NOW - timedelta(days=3),
                "deleted_at": None,
                "flag_status": "to_check" if index == 21 else None,
                "excluded": False,
            }
            entries.append(entry)
            history.append(
                fact(
                    900 + index,
                    id=uid(9000 + index),
                    type="activity_entry",
                    role=None,
                    area=primary,
                    created_at=NOW - timedelta(days=3),
                    started_at=NOW - timedelta(days=3),
                    ended_at=NOW - timedelta(days=3),
                    flag_status="to_check" if index == 21 else None,
                    context={
                        "event": None,
                        "project": None,
                        "result": None,
                        "releases": [],
                        "mentorship": None,
                        "attendance_status": None,
                    },
                )
            )
        if fresh:
            history = [
                fact(
                    index,
                    area=primary,
                    created_at=NOW - timedelta(days=4),
                    started_at=NOW - timedelta(days=3),
                    ended_at=NOW - timedelta(days=2),
                    context={
                        "event": historical_event(catalog["member_meetup"], 3),
                        "project": None,
                        "result": None,
                        "releases": [],
                        "mentorship": None,
                        "attendance_status": "attended",
                    },
                )
            ]
        name = (
            NAMES[index]
            if index < len(NAMES)
            else f"{primary.replace('_', ' ').title()} Participant {index + 1}"
        )
        reviews: list[Payload] = []
        if not beginner and index < 28:
            context = "lead" if index == 0 else "mentor" if index in (4, 15, 23) else "member"
            reviews.append(
                {
                    "id": uid(11000 + index),
                    "reviewer_id": user((index + 1) % 120),
                    "reviewee_id": user(index),
                    "context": context,
                    "rating": 9,
                    "subject_type": "mentorship" if context == "mentor" else "project",
                    "subject_id": uid(6000 + index) if context == "mentor" else uid(3000 + index),
                    "comment": "Explained tradeoffs clearly and followed through on commitments.",
                    "created_at": NOW - timedelta(days=8),
                    "revealed_at": NOW - timedelta(days=1),
                    "removed_at": None,
                    "flag_status": None,
                }
            )
        output[f"{index + 1:03d}_{primary}"] = MemberHistory.model_validate(
            {
                "user_id": user(index),
                "display_name": name,
                "about_md": (
                    f"{'Learning' if beginner else 'Building reliable systems in'} {primary}; "
                    "interested in collaborative projects."
                ),
                "cv_text": f"{name}: practical {primary} experience and community contributions."
                if index % 2 == 0
                else None,
                "github": {
                    "login": f"hmm-persona-{index + 1}",
                    "account_created_at": NOW
                    - timedelta(days=10 if fresh else 1400 if not beginner else 120),
                    "languages": {"Python": 65.0, "Rust": 35.0},
                    "repos": [
                        {
                            "name": f"toolkit-{index + 1}",
                            "description": f"Practical {primary} toolkit",
                            "topics": [primary],
                            "language": "Python",
                            "stars": 0 if beginner else 12,
                            "pushed_at": NOW - timedelta(days=400 if inactive else 3),
                        }
                    ],
                    "followers": 1 if beginner else 25,
                    "contributions_last_year": 0 if inactive else 30 if beginner else 420,
                    "public_repos": 1,
                    "stars_total": 0 if beginner else 12,
                },
                "area_scores": scores,
                "scores_reviewed_at": NOW - timedelta(days=5) if index in (0, 22) else None,
                "xp": 2.0 if fresh else 10.0 if beginner else 1200.0 if inactive else 450.0,
                "activity": 0.0 if inactive else 8.0 if beginner else 30.0,
                "hackathons_finished": 0 if index == 17 else hackathon_count,
                "times_led": 4 if index == 0 else 2 if index == 1 else 0,
                "projects_shipped": 1 if index in (0, 2, 18, 20) else 0,
                "mentorships_completed": 4 if index in (26, 27) else 1 if index in (4, 5) else 0,
                "reputation": {
                    str(review["context"]): {"avg": 9.0, "count": 1} for review in reviews
                },
                "mentor_offers": [
                    {
                        "area": "ml_ai",
                        "topics": "ML evaluation, first Kaggle competition, data leakage",
                        "active": True,
                    }
                ]
                if index in (0, 4, 8, 12, 14, 15, 22, 23, 24, 25, 26)
                else [],
                "mentor_capacity": 3,
                "mentor_active": 3 if index == 15 else 1 if index == 4 else 0,
                "joined_at": NOW - timedelta(days=8 if fresh else 12 if beginner else 800),
                "mentor_notified_at": [NOW - timedelta(hours=2)] if index == 0 else [],
                "onboarding": {
                    "area_scores": {
                        key: {
                            "value": score["calculated"],
                            "reason": "Quiz and public repository evidence",
                        }
                        for key, score in scores.items()
                    },
                    "discrepancies": ["Interview showed more ML depth than the quiz"]
                    if index == 22
                    else [],
                    "submitted_at": NOW - timedelta(days=7 if fresh else 11 if beginner else 799),
                },
                "facts": history,
                "activity_entries": entries,
                "reviews_received": reviews,
                "xp_adjustments_total": -50.0 if index == 16 else 0.0,
            }
        )
    return output


def snapshot(member: MemberHistory) -> MemberSnapshot:
    return MemberSnapshot.model_validate(
        {key: getattr(member, key) for key in MemberSnapshot.model_fields}
    )


def enrolled(member: MemberHistory, index: int, volunteer: bool) -> EnrolledMember:
    return EnrolledMember.model_validate(
        snapshot(member).model_dump()
        | {
            "preferred_area": area_key(index),
            "willing_to_lead": volunteer,
            "enrolled_at": NOW - timedelta(days=5, minutes=-index),
            "note": "Happy to learn alongside an experienced teammate"
            if index in (3, 5, 6, 7, 11)
            else "Can own a reproducible end-to-end slice",
        }
    )


def formations(
    people: list[MemberHistory], catalog: dict[str, EventSnapshot]
) -> dict[str, FormationCase]:
    definitions = [
        (
            "scenario_03_04",
            "Steps 3-4: fourteen people, two intact claimed cores, two reserves",
            "ml_hackathon",
            [[0, 2], [1, 3]],
            list(range(4, 14)),
            {5},
            [6, 6],
            0,
        ),
        (
            "scenario_13",
            "Step 13: one claimed lead plus six enrollments; volunteer seeds the new team",
            "small_hackathon",
            [[2]],
            [5, 6, 7, 8, 9, 10],
            {5},
            [4, 3],
            1,
        ),
        (
            "no_cores",
            "No claims; qualified and below-threshold volunteers seed separate teams",
            "ml_hackathon",
            [],
            list(range(12)),
            {0, 5},
            [6, 6],
            2,
        ),
        (
            "undersized_merge",
            "Intact undersized cores require a merge proposal, not an applied merge",
            "ml_hackathon",
            [[0], [1]],
            [2],
            set(),
            [2, 1],
            0,
        ),
        (
            "no_volunteers",
            "A new team with no volunteers must offer the lead to tier 4",
            "small_hackathon",
            [],
            [2, 6, 7, 9],
            set(),
            [4],
            0,
        ),
        (
            "many_volunteers",
            "Spread volunteers before skill optimization; prefer tier 2 over tier 3",
            "small_hackathon",
            [],
            list(range(12)),
            {0, 1, 2, 4, 5, 8, 9},
            [4, 4, 4],
            3,
        ),
        (
            "late_reserve",
            "Late round considers only unoffered reserve members, not locked teams",
            "small_hackathon",
            [],
            [2, 5, 6, 7, 8, 9, 10],
            {5, 8},
            [4, 3],
            2,
        ),
        (
            "stress_100",
            "Exactly 100 unplaced members exercises the algorithm boundary, with all twelve areas",
            "small_hackathon",
            [],
            list(range(100)),
            set(range(0, 100, 4)),
            [4] * 25,
            25,
        ),
    ]
    output: dict[str, FormationCase] = {}
    for case_index, (
        stem,
        description,
        event_key,
        core_indices,
        free,
        volunteers,
        sizes,
        spread,
    ) in enumerate(definitions):
        event = catalog[event_key]
        maximum = event.team_size_max or 5
        viable = max(event.team_size_min or 2, (maximum * 3 + 3) // 4)
        cores: list[Core] = []
        for core_index, indices in enumerate(core_indices):
            team: list[TeamMemberSnapshot] = []
            for position, index in enumerate(indices):
                team.append(
                    TeamMemberSnapshot.model_validate(
                        snapshot(people[index]).model_dump()
                        | {
                            "role": "lead" if position == 0 else "member",
                            "area": area_key(index),
                            "state": "confirmed",
                            "willing_to_lead": False,
                            "claimed_lead": position == 0,
                            "enrolled_at": NOW - timedelta(days=5, minutes=-index),
                        }
                    )
                )
            cores.append(
                Core(
                    project_id=uid(4000 + case_index * 10 + core_index),
                    lead=team[0],
                    members=team[1:],
                )
            )
        unplaced = [enrolled(people[index], index, index in volunteers) for index in free]
        fits = {
            user(index): LeadFit(
                user_id=user(index),
                fit=0.35
                if index == 5
                else 0.9
                if index == 0
                else 0.8
                if index in (1, 2, 4, 8, 9)
                else 0.5,
                reason="Experienced primary-area contributor"
                if index != 5
                else "New to team delivery; willing to learn leadership",
            )
            for index in free
        }
        request = FormationRequest.model_validate(
            {
                "hackathon": event,
                "kind": "late" if stem == "late_reserve" else "main",
                "target": maximum,
                "viable": viable,
                "max_size": maximum,
                "bounds": placement_bounds(
                    len(free), [len(indices) for indices in core_indices], viable, maximum, maximum
                ),
                "cores": cores,
                "unplaced": unplaced,
                "lead_fits": fits,
            }
        )
        coverage = stem in ("scenario_03_04", "scenario_13", "no_cores", "many_volunteers")
        output[stem] = FormationCase.model_validate(
            {
                "description": description,
                "request": request,
                "expect": {
                    "primary_coverage_per_team": {event.areas[0].area: 1} if coverage else {},
                    "required_coverage_per_team": {"backend": 1, "frontend": 1}
                    if stem in ("scenario_03_04", "no_cores")
                    else {},
                    "fixed_cores": {
                        core.project_id: [
                            core.lead.user_id,
                            *[member.user_id for member in core.members],
                        ]
                        for core in cores
                    },
                    "new_teams_with_volunteers": spread,
                    "team_sizes": sizes,
                    "new_team_lead_tiers": [4]
                    if stem == "no_volunteers"
                    else [3]
                    if stem == "scenario_13"
                    else [2, 3]
                    if stem == "no_cores"
                    else [],
                    "merge_proposal_required": stem == "undersized_merge",
                    "pair_beginners_with_specialists": stem != "undersized_merge",
                },
            }
        )
    return output


def mentor_edge(index: int, mentor: int, learner: int, area: str = "ml_ai") -> MentorshipEdge:
    return MentorshipEdge.model_validate(
        {
            "id": uid(50000 + index),
            "mentor_id": user(mentor),
            "learner_id": user(learner),
            "area": area,
            "kind": "mentoring",
            "outcome": "reached",
            "started_at": NOW - timedelta(days=30 - index * 2),
            "closed_at": NOW - timedelta(days=20 - index * 2),
        }
    )


def mentorships(people: list[MemberHistory]) -> dict[str, MentorshipCase]:
    area = Area.model_validate(
        {
            "id": uid(2003),
            "key": "ml_ai",
            "name": "ML/AI",
            "description": "Machine learning, evaluation and AI systems",
            "sort": 3,
            "archived_at": None,
        }
    )
    definitions = [
        (
            "scenario_15",
            "Step 15: first Kaggle evaluation request, at least five eligible mentors",
            5,
            [0, 4, 8, 12, 22, 24],
            [],
            4,
        ),
        (
            "study_pair",
            "Close effective scores classify the accepted offer as a study pair",
            25,
            [8, 24, 22],
            [],
            24,
        ),
        (
            "capacity_full",
            "Core removes an otherwise strong mentor whose global capacity is full",
            5,
            [4, 8, 12, 22, 24],
            [15],
            4,
        ),
        (
            "restricted_mentor",
            "Core excludes a mentoring-restricted member before building the request",
            5,
            [4, 8, 12, 22, 24],
            [23],
            4,
        ),
        (
            "repeat_reciprocal",
            "Repeat and cross-area reciprocal mentorship history exercises diminishing returns",
            27,
            [4, 8, 24, 26],
            [],
            4,
        ),
        (
            "new_mentor",
            "A strong new mentor is mixed in beside frequently notified veterans",
            11,
            [0, 4, 8, 12, 24],
            [],
            24,
        ),
    ]
    output: dict[str, MentorshipCase] = {}
    for stem, description, learner, candidates, exclusions, ranked in definitions:
        request = MentorMatchRequest.model_validate(
            {
                "area": area,
                "topic": "First Kaggle competition: validation without leakage",
                "goal": "Produce a reproducible held-out evaluation and explain failure cases",
                "format": "online",
                "learner": snapshot(people[learner]),
                "candidates": [snapshot(people[index]) for index in candidates],
                "limit": 5,
            }
        )
        classifications: list[Payload] = []
        for index in candidates:
            score = people[index].area_scores["ml_ai"].effective
            learner_score = people[learner].area_scores["ml_ai"].effective
            classifications.append(
                {
                    "mentor_id": user(index),
                    "learner_id": user(learner),
                    "kind": "mentoring"
                    if score >= 6.0 and score >= learner_score + 2.0
                    else "study_pair",
                }
            )
        output[stem] = MentorshipCase.model_validate(
            {
                "description": description,
                "request": request,
                "expect": {
                    "ranked": [{"user_id": user(ranked), "top_n": min(5, len(candidates))}],
                    "must_not_appear": [user(learner), *[user(index) for index in exclusions]],
                    "classifications": classifications,
                },
                "excluded_candidates": [
                    {
                        "member": snapshot(people[index]),
                        "reason": "capacity_full" if index == 15 else "restricted",
                        "restrictions": [] if index == 15 else ["mentoring"],
                    }
                    for index in exclusions
                ],
                "pair_history": [
                    mentor_edge(0, 26, 27),
                    mentor_edge(1, 26, 27),
                    mentor_edge(2, 27, 26, "backend"),
                ]
                if stem == "repeat_reciprocal"
                else [],
            }
        )
    return output


def edge(index: int, confirmer: int, subject: int, context: UUID, day: int = 0) -> Payload:
    return {
        "id": uid(60000 + index),
        "confirmer_id": user(confirmer),
        "subject_user_id": user(subject),
        "fact_type": "attendance",
        "fact_id": uid(70000 + index),
        "context_id": context,
        "source": "peer",
        "created_at": NOW - timedelta(days=7 - day),
        "ignored": False,
        "withdrawn_at": None,
        "flag_status": None,
    }


def communities(people: list[MemberHistory]) -> dict[str, CommunityCase]:
    output: dict[str, CommunityCase] = {}
    definitions = [
        (
            "clean",
            "Three established members confirm one genuine meetup; diverse accounts and no burst",
            [18, 19, 20],
        ),
        (
            "mutual_confirmation_ring",
            "Closed three-account cluster repeatedly confirms only its own facts",
            [30, 31, 32],
        ),
        (
            "scenario_16",
            "Step 16: two fresh accounts mutually confirm five member-created events in one week",
            [33, 34],
        ),
        (
            "reciprocal_mentoring",
            "The same pair alternates mentor roles across areas over several completed sessions",
            [26, 27],
        ),
        (
            "rating_ring",
            "Three accounts exchange perfect ratings repeatedly in an isolated project cluster",
            [35, 36, 37],
        ),
        (
            "pardoned_pair",
            "The subject/reason pair was pardoned by an admin and must not be flagged again",
            [38, 39],
        ),
    ]
    for case_index, (stem, description, indices) in enumerate(definitions):
        accounts: dict[UUID, Payload] = {}
        for index in [*indices, 0]:
            member = people[index]
            accounts[user(index)] = {
                "joined_at": member.joined_at,
                "xp": member.xp,
                "github_account_age": NOW - member.github.account_created_at
                if member.github
                else None,
                "restrictions": [],
            }
        confirmations: list[Payload] = []
        member_events: list[Payload] = []
        reviews: list[Payload] = []
        mentorship_edges: list[MentorshipEdge] = []
        if stem in ("clean", "mutual_confirmation_ring", "scenario_16", "pardoned_pair"):
            for day in range(1 if stem == "clean" else 5):
                context = uid(80000 + case_index * 100 + day)
                daily: list[Payload] = []
                for position, confirmer in enumerate(indices):
                    subject = indices[(position + 1) % len(indices)]
                    daily.append(
                        edge(
                            case_index * 100 + day * 10 + position, confirmer, subject, context, day
                        )
                    )
                confirmations.extend(daily)
                if stem == "scenario_16":
                    member_events.append(
                        {
                            "id": context,
                            "creator_id": user(indices[day % len(indices)]),
                            "created_at": NOW - timedelta(days=7 - day, hours=1),
                            "starts_at": NOW - timedelta(days=7 - day, minutes=45),
                            "ends_at": NOW - timedelta(days=7 - day, minutes=15),
                            "attendees": [user(index) for index in indices],
                            "confirmations": daily,
                        }
                    )
        if stem == "reciprocal_mentoring":
            mentorship_edges = [
                mentor_edge(
                    index + 3,
                    26 if index % 2 == 0 else 27,
                    27 if index % 2 == 0 else 26,
                    "ml_ai" if index % 2 == 0 else "backend",
                )
                for index in range(6)
            ]
        if stem == "rating_ring":
            for day in range(5):
                for source in indices:
                    for target in indices:
                        if source != target:
                            reviews.append(
                                {
                                    "id": uid(
                                        90000 + day * 100 + source * 3 + indices.index(target)
                                    ),
                                    "reviewer_id": user(source),
                                    "reviewee_id": user(target),
                                    "context": "member",
                                    "rating": 10,
                                    "subject_type": "project",
                                    "subject_id": uid(91000 + day),
                                    "created_at": NOW - timedelta(days=day + 8),
                                    "revealed_at": NOW - timedelta(days=day + 1),
                                    "removed_at": None,
                                    "flag_status": None,
                                }
                            )
        reason = "Repeated mutual confirmations in a closed pair"
        pardons: list[Payload] = (
            [{"subject_type": "ring", "subject_id": uid(92000), "reason": reason}]
            if stem == "pardoned_pair"
            else []
        )
        if pardons:
            for item in confirmations:
                item["flag_status"] = "pardoned"
                pardons.append(
                    {"subject_type": "confirmation", "subject_id": item["id"], "reason": reason}
                )
        graph = CommunityGraph.model_validate(
            {
                "confirmations": confirmations,
                "mentorships": mentorship_edges,
                "reviews": reviews,
                "member_events": member_events,
                "accounts": accounts,
                "pardoned": pardons,
            }
        )
        output[stem] = CommunityCase.model_validate(
            {
                "description": description,
                "graph": graph,
                "expect_flagged": []
                if stem in ("clean", "pardoned_pair")
                else [set(user(index) for index in indices)],
                "expect_not_flagged": [user(0), *[user(index) for index in indices]]
                if stem in ("clean", "pardoned_pair")
                else [user(0)],
                "pardoned_pairs": [
                    {"user_ids": [user(38), user(39)], "subject_id": uid(92000), "reason": reason}
                ]
                if pardons
                else [],
            }
        )
    return output


def visibility() -> dict[str, VisibilityCase]:
    output: dict[str, VisibilityCase] = {}

    def add(
        stem: str,
        description: str,
        role: str,
        kind: str,
        visible: bool,
        subject: Payload | None = None,
        viewer: Payload | None = None,
        fields: list[str] | None = None,
        operation: str = "read",
    ) -> None:
        output[stem] = VisibilityCase.model_validate(
            {
                "description": description,
                "operation": operation,
                "viewer": {
                    "role": role,
                    "user_id": None if role == "anonymous" else user(5),
                    **(viewer or {}),
                },
                "subject": {"kind": kind, "id": uid(95000 + len(output)), **(subject or {})},
                "expect_visible": visible,
                "visible_fields": fields,
            }
        )

    add(
        "anonymous_profile",
        "Profiles require completed member authentication",
        "anonymous",
        "profile",
        False,
    )
    add(
        "onboarding_gate",
        "Incomplete onboarding cannot access the member directory",
        "onboarding",
        "profile",
        False,
        viewer={"onboarding_status": "in_progress"},
    )
    add(
        "onboarding_own_profile",
        "The onboarding gate permits editing the user's own profile",
        "onboarding",
        "own_profile",
        True,
        subject={"owner_id": user(5)},
        viewer={"onboarding_status": "in_progress"},
        operation="edit",
    )
    add(
        "onboarding_cv",
        "Uploading one's CV is allowed before onboarding completes",
        "onboarding",
        "cv",
        True,
        subject={"owner_id": user(5)},
        viewer={"onboarding_status": "not_started"},
        operation="upload",
    )
    add(
        "onboarding_notifications",
        "Notifications remain accessible during onboarding",
        "onboarding",
        "notifications",
        True,
        viewer={"onboarding_status": "reopened"},
    )
    add(
        "banned_member",
        "Bans override otherwise valid membership",
        "banned",
        "profile",
        False,
        viewer={"banned": True},
    )
    add(
        "banned_admin",
        "Admin privileges never bypass a ban",
        "admin",
        "project",
        False,
        viewer={"banned": True},
    )
    add(
        "member_cv",
        "Every onboarded member can read another member's CV",
        "member",
        "cv",
        True,
        subject={"owner_id": user(0)},
    )
    add(
        "dnd_profile",
        "DND is availability, not profile privacy",
        "member",
        "profile",
        True,
        subject={"dnd": True},
    )
    for kind in ("mentor_offers", "mentor_matching", "quick_pick"):
        add(
            f"dnd_{kind}",
            "DND hides offers and excludes unsolicited recommendations",
            "member",
            kind,
            False,
            subject={"dnd": True},
        )
    add(
        "dnd_enrolled_formation",
        "Self-enrolled DND members still participate in formation",
        "member",
        "formation",
        True,
        subject={"dnd": True},
    )
    for role in ("member", "team_member", "admin"):
        add(
            f"retrospective_{role}",
            "Retrospectives are restricted to the team and admins",
            role,
            "retrospective",
            role != "member",
            subject={"project_id": uid(4000)},
            viewer={"project_id": uid(4000) if role == "team_member" else None},
        )
    add(
        "review_blind",
        "A reviewee cannot see windowed reviews until close",
        "reviewee",
        "review",
        False,
        subject={"review_window_closed": False},
    )
    add(
        "review_revealed",
        "Closed-window reviews are visible to members",
        "member",
        "review",
        True,
        subject={"review_window_closed": True},
    )
    add(
        "review_author",
        "The reviewer can read and edit their own blind submission",
        "reviewer",
        "review",
        True,
        subject={"owner_id": user(5), "review_window_closed": False},
    )
    add(
        "public_project",
        "Public project details are visible to all members, not anonymous users",
        "member",
        "project",
        True,
        subject={"visibility": "public"},
        fields=["name", "areas", "goal", "description_md", "links"],
    )
    add(
        "anonymous_public_project",
        "Project public visibility does not bypass member authentication",
        "anonymous",
        "project",
        False,
        subject={"visibility": "public"},
    )
    add(
        "private_project_listing",
        "Members-only projects expose only name and areas to outsiders",
        "member",
        "project",
        True,
        subject={"visibility": "members", "project_id": uid(4000)},
        fields=["name", "areas"],
    )
    add(
        "private_project_team",
        "Project members can see private project details",
        "team_member",
        "project",
        True,
        subject={"visibility": "members", "project_id": uid(4000)},
        viewer={"project_id": uid(4000)},
        fields=["name", "areas", "goal", "description_md", "links"],
    )
    for role in ("member", "learner", "target_mentor", "admin"):
        add(
            f"direct_request_{role}",
            "Direct requests are visible only to the learner and target, not unrelated admins",
            role,
            "direct_help_request",
            role in ("learner", "target_mentor"),
            subject={"learner_id": user(5), "target_mentor_id": user(4)},
            viewer={
                "user_id": user(4)
                if role == "target_mentor"
                else user(5)
                if role == "learner"
                else user(9)
            },
        )
    for kind in ("pending_invites", "pending_requests", "pending_offers"):
        for role in ("member", "team_member", "lead", "admin"):
            add(
                f"{kind}_{role}",
                "Project pending lists are visible only to that project's lead and admins",
                role,
                kind,
                role in ("lead", "admin"),
                subject={"project_id": uid(4000), "pending": True},
                viewer={"project_id": uid(4000) if role in ("lead", "team_member") else None},
            )
        add(
            f"{kind}_unrelated_lead",
            "Leading another project grants no rights to this project's pending lists",
            "lead",
            kind,
            False,
            subject={"project_id": uid(4000), "pending": True},
            viewer={"project_id": uid(4010)},
        )
    add(
        "flagged_fact",
        "To-check facts are hidden from public profile history",
        "member",
        "facts",
        False,
        subject={"to_check": True},
    )
    add(
        "excluded_fact",
        "Punished excluded facts stay hidden",
        "member",
        "facts",
        False,
        subject={"excluded": True},
    )
    return output


def generate(destination: Path) -> None:
    catalog = events()
    personas = members(catalog)
    people = list(personas.values())
    groups: dict[str, dict[str, Contract]] = {
        "members": dict(personas),
        "events": dict(catalog),
        "formation": dict(formations(people, catalog)),
        "mentorship": dict(mentorships(people)),
        "community": dict(communities(people)),
        "visibility": dict(visibility()),
    }
    for group, cases in groups.items():
        directory = destination / group
        directory.mkdir(parents=True, exist_ok=True)
        for stem, case in sorted(cases.items()):
            content = (
                json.dumps(
                    case.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True
                )
                + "\n"
            )
            (directory / f"{stem}.json").write_text(content, encoding="utf-8")
    generate_notifications(destination)


def generate_notifications(destination: Path) -> None:
    entity_fields = {
        ActionKey.INVITE_ACCEPT: "invite_id",
        ActionKey.INVITE_DECLINE: "invite_id",
        ActionKey.JOIN_REQUEST_ACCEPT: "join_request_id",
        ActionKey.JOIN_REQUEST_REJECT: "join_request_id",
        ActionKey.PLACEMENT_CONFIRM: "project_id",
        ActionKey.PLACEMENT_DECLINE: "project_id",
        ActionKey.PROJECT_LEAVE: "project_id",
        ActionKey.LEAD_OFFER_ACCEPT: "lead_offer_id",
        ActionKey.LEAD_OFFER_DECLINE: "lead_offer_id",
        ActionKey.EVENT_ENROLL: "event_id",
        ActionKey.SWAP_ACCEPT: "swap_request_id",
        ActionKey.SWAP_DECLINE: "swap_request_id",
        ActionKey.MERGE_ACCEPT: "merge_proposal_id",
        ActionKey.MERGE_DECLINE: "merge_proposal_id",
        ActionKey.LEAD_STEP_DOWN: "project_id",
        ActionKey.PROJECT_CLAIM_LEAD: "project_id",
        ActionKey.HELP_OFFER_ACCEPT: "help_offer_id",
        ActionKey.HELP_REQUEST_DECLINE: "help_request_id",
        ActionKey.HELP_REQUEST_OPEN: "help_request_id",
        ActionKey.MENTORSHIP_ACKNOWLEDGE: "mentorship_id",
    }
    link_labels = {
        NotificationType.FORMATION_READY: ("open_team",),
        NotificationType.REVIEW_ENDING: ("open_team",),
        NotificationType.SEAT_OPENED: ("open_team",),
        NotificationType.LEAD_OPPORTUNITY: ("claim_lead",),
        NotificationType.LEAD_ASSIGNED: ("transfer",),
        NotificationType.ATTENDANCE_CONFIRM_DIGEST: ("confirm_attendees",),
        NotificationType.PROJECT_IDLE: ("open_project",),
        NotificationType.PROJECT_PAUSED: ("open_project",),
        NotificationType.CONTRIBUTIONS_CONFIRM_DIGEST: ("confirm_lead",),
        NotificationType.HELP_REQUEST_MATCH: ("help",),
        NotificationType.HELP_REQUEST_DIRECT: ("help",),
        NotificationType.HELP_REQUEST_EXPIRED: ("reopen",),
        NotificationType.MENTORSHIP_IDLE: ("check_in",),
        NotificationType.MENTORSHIP_STALLED: ("check_in",),
        NotificationType.MENTORSHIP_CLOSE_PROPOSED: ("close_with_outcome",),
        NotificationType.MENTORSHIP_CLOSED: ("endorse",),
        NotificationType.RESULT_DUE: ("submit_result",),
        NotificationType.REVIEWS_OPEN: ("review",),
        NotificationType.REVIEWS_PENDING: ("review",),
        NotificationType.ADMIN_ALERT: ("open_alert",),
    }
    directory = destination / "notifications"
    directory.mkdir(parents=True, exist_ok=True)
    for source in sorted((Path(__file__).resolve().parent / "notifications").glob("*.json")):
        data = json.loads(source.read_text(encoding="utf-8"))
        kind = NotificationType(data["type"])
        payload = data["payload"]
        actions: list[ApiAction | LinkAction] = [
            ApiAction.model_validate(
                {
                    "key": key,
                    "entity_id": payload[entity_fields[key]],
                    "label_key": f"notifications.actions.{key}",
                }
            )
            for key in ALLOWED_ACTIONS[kind]
        ]
        url = payload["link"]
        if kind in (NotificationType.HELP_REQUEST_MATCH, NotificationType.HELP_REQUEST_DIRECT):
            url = "/mentorship"
        elif kind == NotificationType.HELP_REQUEST_EXPIRED:
            url = f"/mentorship/new?from={payload['help_request_id']}"
        actions.extend(
            LinkAction(label_key=f"notifications.actions.{label}", url=url)
            for label in link_labels.get(kind, ())
        )
        payload["actions"] = [action.model_dump(mode="json") for action in actions]
        notification = Notification.model_validate(data)
        (directory / source.name).write_text(
            json.dumps(notification.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    arguments = parser.parse_args()
    generate(arguments.output_dir)


if __name__ == "__main__":
    main()
