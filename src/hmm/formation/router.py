from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from hmm.common import EnrollmentStatus, ErrorCode, InviteStatus
from hmm.formation import admin as formation_admin
from hmm.formation import common, queue, schemas, teams
from hmm.formation import invites as formation_invites
from hmm.formation import parties as formation_parties
from hmm.formation import runs as formation_runs
from hmm.platform.clock import CurrentClock
from hmm.platform.db import Session
from hmm.platform.errors import PROBLEM_RESPONSES, ProblemError, problem_response
from hmm.platform.pagination import CursorParams
from hmm.platform.schemas import ReasonBody
from hmm.platform.security import AdminAuth, MemberAuth

router = APIRouter(tags=["formation"], responses=PROBLEM_RESPONSES)


@router.get("/events/{id}/formation")
async def get_formation(
    id: UUID, actor: MemberAuth, session: Session, clock: CurrentClock
) -> schemas.Formation:
    event = await common.formation_event(session, id, actor)
    view = await queue.formation_participation(session, event, actor, clock.now())
    if view is None:
        raise ProblemError(ErrorCode.NOT_FOUND)
    return view


@router.post("/events/{id}/enroll", status_code=201)
async def enroll(
    id: UUID,
    body: schemas.CreateEnrollmentBody,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
) -> schemas.Enrollment:
    return await queue.enroll(session, id, body, actor, clock.now())


@router.patch("/events/{id}/enroll")
async def update_enrollment(
    id: UUID,
    body: schemas.UpdateEnrollmentBody,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
) -> schemas.Enrollment:
    return await queue.update_enrollment(session, id, body, actor, clock.now())


@router.delete("/events/{id}/enroll", status_code=204)
async def withdraw_enrollment(
    id: UUID, actor: MemberAuth, session: Session, clock: CurrentClock
) -> Response:
    await queue.withdraw_enrollment(session, id, actor, clock.now())
    return Response(status_code=204)


@router.get("/events/{id}/enrollments")
async def list_enrollments(
    id: UUID, actor: MemberAuth, session: Session, params: Annotated[CursorParams, Depends()]
) -> schemas.EnrollmentPage:
    return await queue.list_enrollments(session, id, actor, params)


@router.get("/me/enrollments")
async def my_enrollments(
    actor: MemberAuth, session: Session, status: Annotated[EnrollmentStatus | None, Query()] = None
) -> list[schemas.Enrollment]:
    return await queue.my_enrollments(session, actor, status)


@router.post("/events/{id}/party", status_code=201)
async def create_party(
    id: UUID, actor: MemberAuth, session: Session, clock: CurrentClock
) -> schemas.Party:
    return await formation_parties.create_party(session, id, actor, clock.now())


@router.get("/parties/{id}")
async def get_party(id: UUID, actor: MemberAuth, session: Session) -> schemas.Party:
    return await formation_parties.get_party(session, id, actor)


@router.patch("/parties/{id}")
async def update_party(
    id: UUID, body: schemas.ClosedBody, actor: MemberAuth, session: Session, clock: CurrentClock
) -> schemas.Party:
    return await formation_parties.update_party(session, id, body, actor, clock.now())


@router.post("/parties/{id}/invites", status_code=201)
async def create_party_invite(
    id: UUID, body: schemas.InviteBody, actor: MemberAuth, session: Session, clock: CurrentClock
) -> schemas.Invite:
    return await formation_invites.create_party_invite(session, id, body, actor, clock.now())


@router.post("/parties/{id}/leave", status_code=204)
async def leave_party(
    id: UUID, actor: MemberAuth, session: Session, clock: CurrentClock
) -> Response:
    await formation_parties.leave_party(session, id, actor, clock.now())
    return Response(status_code=204)


@router.delete("/parties/{id}/members/{user_id}", status_code=204)
async def kick_party_member(
    id: UUID, user_id: UUID, actor: MemberAuth, session: Session, clock: CurrentClock
) -> Response:
    await formation_parties.kick_party_member(session, id, user_id, actor, clock.now())
    return Response(status_code=204)


@router.get("/me/invites")
async def my_invites(
    actor: MemberAuth, session: Session, status: Annotated[InviteStatus | None, Query()] = None
) -> list[schemas.Invite]:
    return await formation_invites.my_invites(session, actor, status)


@router.post("/projects/{id}/placement/accept")
async def accept_placement(
    id: UUID, actor: MemberAuth, session: Session, clock: CurrentClock, request: Request
) -> schemas.Team:
    result = await teams.accept_placement(session, id, actor, clock.now())
    if isinstance(result, ProblemError):
        return cast(schemas.Team, problem_response(request, result.code, result.detail))
    return result


@router.post("/projects/{id}/placement/decline")
async def decline_placement(
    id: UUID,
    body: schemas.DeclinePlacementBody,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
    request: Request,
) -> schemas.Team:
    result = await teams.decline_placement(session, id, body, actor, clock.now())
    if isinstance(result, ProblemError):
        return cast(schemas.Team, problem_response(request, result.code, result.detail))
    return result


@router.patch("/projects/{id}/seats")
async def set_seats(
    id: UUID, body: schemas.ClosedBody, actor: MemberAuth, session: Session, clock: CurrentClock
) -> schemas.Team:
    return await teams.set_seats(session, id, body, actor, clock.now())


@router.patch("/projects/{id}/members/{user_id}")
async def update_member_area(
    id: UUID,
    user_id: UUID,
    body: schemas.UpdateMemberBody,
    actor: MemberAuth,
    session: Session,
    clock: CurrentClock,
) -> schemas.Team:
    return await teams.update_member_area(session, id, user_id, body, actor, clock.now())


@router.patch("/admin/events/{id}/formation")
async def update_formation(
    id: UUID,
    body: schemas.UpdateFormationBody,
    actor: AdminAuth,
    session: Session,
    clock: CurrentClock,
) -> schemas.Formation:
    return await formation_runs.update_formation(session, id, body, actor, clock.now())


@router.post("/admin/events/{id}/formation/run", status_code=202)
async def request_formation_run(
    id: UUID,
    actor: AdminAuth,
    session: Session,
    clock: CurrentClock,
    request: Request,
    body: ReasonBody | None = None,
) -> schemas.FormationRun:
    return await formation_runs.request_formation_run(
        session, request.app.state.jobs, id, body, actor, clock.now()
    )


@router.get("/admin/events/{id}/formation/runs")
async def list_formation_runs(
    id: UUID, actor: AdminAuth, session: Session
) -> list[schemas.FormationRun]:
    return await formation_runs.list_formation_runs(session, id, actor)


@router.post("/admin/projects/{id}/members")
async def admin_add_member(
    id: UUID, body: schemas.InviteBody, actor: AdminAuth, session: Session, clock: CurrentClock
) -> schemas.Team:
    return await formation_admin.admin_add_member(session, id, body, actor, clock.now())


@router.delete("/admin/projects/{id}/members/{user_id}")
async def admin_remove_member(
    id: UUID,
    user_id: UUID,
    actor: AdminAuth,
    session: Session,
    clock: CurrentClock,
    body: ReasonBody | None = None,
) -> schemas.Team:
    return await formation_admin.admin_remove_member(
        session, id, user_id, actor, clock.now(), body.reason if body else None
    )


@router.put("/admin/projects/{id}/lead")
async def admin_set_lead(
    id: UUID,
    body: schemas.TransferLeadBody,
    actor: AdminAuth,
    session: Session,
    clock: CurrentClock,
) -> schemas.Team:
    return await formation_admin.admin_set_lead(session, id, body, actor, clock.now())


@router.post("/admin/projects/{id}/disband")
async def admin_disband(
    id: UUID, body: ReasonBody, actor: AdminAuth, session: Session, clock: CurrentClock
) -> schemas.Team:
    return await formation_admin.admin_disband(session, id, body, actor, clock.now())
