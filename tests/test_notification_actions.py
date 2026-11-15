from datetime import timedelta

import pytest
from factories import formed, stored_member
from harness import Harness

from hmm import models
from hmm.common import ErrorCode
from hmm.formation.schemas import Team
from hmm.identity.service import telegram_actor
from hmm.notifications.actions import ACTION_HANDLERS, run_action
from hmm.notifications.payloads import ActionKey
from hmm.platform.errors import ProblemError


def test_every_action_has_a_handler() -> None:
    assert set(ACTION_HANDLERS) == set(ActionKey)


async def link(h: Harness, user: models.User, telegram_user_id: int, broken: bool) -> None:
    async with h.db() as session:
        session.add(
            models.TelegramLink(
                user_id=user.id,
                telegram_user_id=telegram_user_id,
                chat_id=telegram_user_id,
                username=None,
                linked_at=h.now - timedelta(days=1),
                broken_at=h.now if broken else None,
            )
        )


async def test_telegram_actor(harness: Harness) -> None:
    h = harness
    member = await h.user()
    newcomer = await h.user(onboarded=False)
    banned = await h.user(banned_at=h.now)
    await link(h, member, 1, broken=True)
    await link(h, newcomer, 2, broken=False)
    await link(h, banned, 3, broken=False)
    async with h.db() as session:
        assert (await telegram_actor(session, 1, h.now)).id == member.id
        assert (await telegram_actor(session, 2, h.now, member=False)).id == newcomer.id
        for telegram_user_id, code in (
            (2, ErrorCode.ONBOARDING_REQUIRED),
            (3, ErrorCode.BANNED),
            (4, ErrorCode.UNAUTHENTICATED),
        ):
            with pytest.raises(ProblemError) as raised:
                await telegram_actor(session, telegram_user_id, h.now)
            assert raised.value.code == code
    async with h.db() as session:
        stored = await session.get(models.TelegramLink, member.id)
        assert stored is not None
        assert stored.broken_at is None


async def test_run_action_accepts_placement(harness: Harness) -> None:
    h = harness
    admin = await h.admin()
    _, users, team = await formed(h, admin)
    await link(h, users[0], 10, broken=False)
    async with h.db() as session:
        actor = await telegram_actor(session, 10, h.now)
        result = await run_action(
            session, h.jobs, actor, ActionKey.PLACEMENT_ACCEPT, team.id, h.now
        )
    assert isinstance(result, Team)
    assert (await stored_member(h, team, users[0])).state == "confirmed"
