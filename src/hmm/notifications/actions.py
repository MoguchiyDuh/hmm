from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

import procrastinate
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.formation import queue as formation_queue
from hmm.formation import schemas as formation_schemas
from hmm.formation import teams as formation_teams
from hmm.mentorship import help_offers
from hmm.mentorship import mentorships as mentorship_mentorships
from hmm.mentorship import requests as mentorship_requests
from hmm.mentorship import schemas as mentorship_schemas
from hmm.notifications.payloads import ACTION_ROUTES, ActionKey
from hmm.platform.errors import ProblemError
from hmm.projects import invites, join_requests

type ActionResult = BaseModel | ProblemError
type ActionHandler = Callable[
    [AsyncSession, procrastinate.App, models.User, UUID, datetime], Awaitable[ActionResult]
]


def body[T: BaseModel](key: ActionKey, schema: type[T]) -> T:
    return schema.model_validate(ACTION_ROUTES[key].body or {})


ACTION_HANDLERS: dict[ActionKey, ActionHandler] = {
    ActionKey.INVITE_ACCEPT: lambda session, jobs, actor, id, now: invites.respond_invite(
        session, id, actor, now, "accepted"
    ),
    ActionKey.INVITE_DECLINE: lambda session, jobs, actor, id, now: invites.respond_invite(
        session, id, actor, now, "declined"
    ),
    ActionKey.JOIN_REQUEST_ACCEPT: lambda session, jobs, actor, id, now: (
        join_requests.respond_join_request(session, jobs, id, actor, now, "accepted")
    ),
    ActionKey.JOIN_REQUEST_REJECT: lambda session, jobs, actor, id, now: (
        join_requests.respond_join_request(session, jobs, id, actor, now, "rejected")
    ),
    ActionKey.PLACEMENT_ACCEPT: lambda session, jobs, actor, id, now: (
        formation_teams.accept_placement(session, id, actor, now)
    ),
    ActionKey.PLACEMENT_DECLINE: lambda session, jobs, actor, id, now: (
        formation_teams.decline_placement(
            session,
            id,
            body(ActionKey.PLACEMENT_DECLINE, formation_schemas.DeclinePlacementBody),
            actor,
            now,
        )
    ),
    ActionKey.EVENT_ENROLL: lambda session, jobs, actor, id, now: formation_queue.enroll(
        session,
        id,
        body(ActionKey.EVENT_ENROLL, formation_schemas.CreateEnrollmentBody),
        actor,
        now,
    ),
    ActionKey.HELP_OFFER_ACCEPT: lambda session, jobs, actor, id, now: (
        help_offers.accept_help_offer(session, actor, id, now)
    ),
    ActionKey.HELP_REQUEST_DECLINE: lambda session, jobs, actor, id, now: (
        mentorship_requests.decline_help_request(session, jobs, actor, id, now)
    ),
    ActionKey.HELP_REQUEST_OPEN: lambda session, jobs, actor, id, now: (
        mentorship_requests.open_help_request(session, jobs, actor, id, now)
    ),
    ActionKey.MENTORSHIP_ACKNOWLEDGE: lambda session, jobs, actor, id, now: (
        mentorship_mentorships.acknowledge_outcome(
            session,
            jobs,
            actor,
            id,
            body(ActionKey.MENTORSHIP_ACKNOWLEDGE, mentorship_schemas.AcknowledgeMentorshipBody),
            now,
        )
    ),
}


async def run_action(
    session: AsyncSession,
    jobs: procrastinate.App,
    actor: models.User,
    key: ActionKey,
    entity_id: UUID,
    now: datetime,
) -> ActionResult:
    return await ACTION_HANDLERS[key](session, jobs, actor, entity_id, now)
