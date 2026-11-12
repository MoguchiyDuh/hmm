import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import hmm_contracts.fixtures as fixtures
import pytest
from hmm_contracts.algo import MemberSnapshot
from hmm_contracts.common import SEED_AREA_KEYS, Contract
from hmm_contracts.formation import placement_bounds


@pytest.mark.parametrize(
    ("loader", "minimum"),
    [
        (fixtures.load_members, 24),
        (fixtures.load_events, 8),
        (fixtures.load_formation_cases, 8),
        (fixtures.load_mentorship_cases, 6),
        (fixtures.load_community_cases, 6),
        (fixtures.load_visibility_cases, 12),
        (fixtures.load_imports, 6),
        (fixtures.load_notifications, 1),
    ],
)
def test_loaders_validate_sorted_fixtures(
    loader: Callable[[], dict[str, Contract]], minimum: int
) -> None:
    cases = loader()
    assert len(cases) >= minimum
    assert list(cases) == sorted(cases)
    assert all(isinstance(case, Contract) for case in cases.values())


def test_formation_bounds_and_expectations() -> None:
    for case in fixtures.load_formation_cases().values():
        request = case.request
        sizes = [1 + len(core.members) for core in request.cores]
        assert request.bounds == placement_bounds(
            len(request.unplaced), sizes, request.viable, request.target, request.max_size
        )
        assert len(case.expect.team_sizes) == request.bounds.teams
        assert sum(case.expect.team_sizes) == sum(sizes) + request.bounds.placed
        assert all(size <= request.max_size for size in case.expect.team_sizes)
        assert (
            sum(size < request.viable for size in case.expect.team_sizes)
            == request.bounds.undersized_cores
        )
        assert case.expect.fixed_cores == {
            core.project_id: [core.lead.user_id, *[member.user_id for member in core.members]]
            for core in request.cores
        }
        assert case.expect.new_teams_with_volunteers == min(
            request.bounds.new_teams,
            sum(member.willing_to_lead for member in request.unplaced),
        )


def assert_persona(member: MemberSnapshot, members: dict[UUID, MemberSnapshot]) -> None:
    assert member.user_id in members
    assert (
        MemberSnapshot.model_validate(
            {key: getattr(member, key) for key in MemberSnapshot.model_fields}
        )
        == members[member.user_id]
    )


def test_formation_and_mentorship_share_personas() -> None:
    members = {
        member.user_id: MemberSnapshot.model_validate(
            {key: getattr(member, key) for key in MemberSnapshot.model_fields}
        )
        for member in fixtures.load_members().values()
    }
    assert len(members) == len(fixtures.load_members())
    for case in fixtures.load_formation_cases().values():
        referenced: list[UUID] = []
        for member in case.request.unplaced:
            assert_persona(member, members)
            referenced.append(member.user_id)
        for core in case.request.cores:
            for member in [core.lead, *core.members]:
                assert_persona(member, members)
                referenced.append(member.user_id)
        assert len(referenced) == len(set(referenced))
        assert set(case.request.lead_fits) == {member.user_id for member in case.request.unplaced}
        assert all(key == fit.user_id for key, fit in case.request.lead_fits.items())
        assert all(
            user_id in members for users in case.expect.fixed_cores.values() for user_id in users
        )
    for case in fixtures.load_mentorship_cases().values():
        for member in [case.request.learner, *case.request.candidates]:
            assert_persona(member, members)
        candidate_ids = {member.user_id for member in case.request.candidates}
        assert len(candidate_ids) == len(case.request.candidates)
        assert candidate_ids.isdisjoint(case.expect.must_not_appear)
        assert all(user_id in members for user_id in case.expect.must_not_appear)
        assert all(rank.user_id in candidate_ids for rank in case.expect.ranked)
        for exclusion in case.excluded_candidates:
            assert_persona(exclusion.member, members)
            assert exclusion.member.user_id in case.expect.must_not_appear
            if exclusion.reason == "capacity_full":
                assert exclusion.member.mentor_active >= exclusion.member.mentor_capacity
            if exclusion.reason == "restricted":
                assert "mentoring" in exclusion.restrictions
        for pair in case.expect.classifications:
            assert pair.learner_id == case.request.learner.user_id
            assert pair.mentor_id in candidate_ids
            score = members[pair.mentor_id].area_scores[case.request.area.key].effective
            learner_score = case.request.learner.area_scores[case.request.area.key].effective
            expected = (
                "mentoring" if score >= 6.0 and score >= learner_score + 2.0 else "study_pair"
            )
            assert pair.kind == expected
        for history in case.pair_history:
            assert history.mentor_id in members
            assert history.learner_id in members


def test_personas_cover_scoring_inputs() -> None:
    members = list(fixtures.load_members().values())
    assert {area for member in members for area in member.area_scores} == set(SEED_AREA_KEYS)
    facts = [fact for member in members for fact in member.facts]
    assert {str(fact.trust) for fact in facts} == {"self", "peer", "trusted"}
    assert any(fact.flag_status == "to_check" for fact in facts)
    assert any(fact.context.mentorship for fact in facts)
    assert any(fact.context.releases for fact in facts)
    assert any(member.activity_entries for member in members)
    assert any(member.activity == 0 and member.hackathons_finished >= 5 for member in members)
    assert any(member.mentor_active == member.mentor_capacity for member in members)
    assert any(
        score.override_evidence for member in members for score in member.area_scores.values()
    )
    assert {str(fact.context.result.place_status) for fact in facts if fact.context.result} >= {
        "verified",
        "unverified",
    }
    assert any(
        not participated
        for fact in facts
        if fact.context.result
        for participated in fact.context.result.participants.values()
    )
    for member in members:
        assert member.hackathons_finished == sum(
            fact.context.result is not None
            and fact.context.result.participants.get(member.user_id, False)
            for fact in member.facts
        )
        for fact in member.facts:
            assert member.joined_at <= fact.created_at <= fact.started_at
            if fact.ended_at:
                assert fact.started_at <= fact.ended_at
            assert all(item.confirmer_id != member.user_id for item in fact.confirmations)
            if fact.type == "activity_entry":
                assert fact.trust == "self"
                assert not fact.confirmations


def test_event_ids_identify_consistent_snapshots() -> None:
    events = {event.id: event for event in fixtures.load_events().values()}
    for member in fixtures.load_members().values():
        for fact in member.facts:
            if fact.context.event:
                event = fact.context.event
                assert event.id not in events or events[event.id] == event
                events[event.id] = event
                if fact.context.project:
                    assert fact.context.project.event_id == event.id


def test_community_expectations_and_pardons() -> None:
    for case in fixtures.load_community_cases().values():
        accounts = set(case.graph.accounts)
        flagged = set().union(*case.expect_flagged)
        assert flagged <= accounts
        assert set(case.expect_not_flagged) <= accounts
        assert flagged.isdisjoint(case.expect_not_flagged)
        for pair in case.pardoned_pairs:
            assert set(pair.user_ids) <= accounts
            assert set(pair.user_ids) <= set(case.expect_not_flagged)
            assert any(
                flag.subject_type == "ring"
                and flag.subject_id == pair.subject_id
                and flag.reason == pair.reason
                for flag in case.graph.pardoned
            )
        for confirmation in case.graph.confirmations:
            assert confirmation.confirmer_id in accounts
            assert confirmation.subject_user_id in accounts
            assert confirmation.confirmer_id != confirmation.subject_user_id
            assert (
                case.graph.accounts[confirmation.subject_user_id].joined_at
                <= confirmation.created_at
            )
        for event in case.graph.member_events:
            assert event.creator_id in accounts
            assert set(event.attendees) <= accounts
            assert event.created_at <= event.starts_at <= event.ends_at


def test_generator_reproduces_committed_fixtures(tmp_path: Path) -> None:
    directory = fixtures.fixtures_dir()
    script = directory / "generate.py"
    groups = ("members", "events", "formation", "mentorship", "community", "visibility")
    expected = {
        path.relative_to(directory): path.read_bytes()
        for group in groups
        for path in (directory / group).glob("*.json")
    }
    for _ in range(2):
        subprocess.run(
            [sys.executable, str(script), "--output-dir", str(tmp_path)],
            cwd=directory.parents[1],
            check=True,
            capture_output=True,
            text=True,
        )
        actual = {
            path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*.json")
        }
        assert actual == expected


def test_missing_fixtures_directory_has_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        fixtures, "__file__", str(tmp_path / "src" / "hmm_contracts" / "fixtures.py")
    )
    with pytest.raises(FileNotFoundError, match="editable repository install"):
        fixtures.fixtures_dir()
