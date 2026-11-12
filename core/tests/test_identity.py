import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import FastAPI
from hmm_contracts.common import ActorKind, AuditAction, ErrorCode, OnboardingStatus
from hmm_core.app import create_app
from hmm_core.clock import FixedClock
from hmm_core.config import Config
from hmm_core.db import create_sessionmaker
from hmm_core.db.models import AuditLog, User, UserSession
from hmm_core.github import TOKEN_URL, USER_URL
from hmm_core.identity import hash_token, pkce_challenge
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from test_api_skeleton import assert_problem

PUBLIC_URL = "https://hmm.test"
NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


class FakeGithub:
    def __init__(self) -> None:
        self.grants: dict[str, tuple[str, dict[str, Any]]] = {}
        self.tokens: dict[str, dict[str, Any]] = {}

    def authorize(self, location: httpx.URL, profile: dict[str, Any]) -> str:
        params = location.params
        assert str(location).startswith("https://github.com/login/oauth/authorize?")
        assert params["client_id"] == "client-id"
        assert params["redirect_uri"] == f"{PUBLIC_URL}/api/v1/auth/github/callback"
        assert params["scope"] == "read:user"
        assert params["code_challenge_method"] == "S256"
        code = secrets.token_hex(8)
        self.grants[code] = (params["code_challenge"], profile)
        return code

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url == TOKEN_URL:
            form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            assert form["client_secret"] == "client-secret"
            challenge, profile = self.grants.pop(form["code"])
            if pkce_challenge(form["code_verifier"]) != challenge:
                return httpx.Response(200, json={"error": "bad_verification_code"})
            token = secrets.token_hex(8)
            self.tokens[token] = profile
            return httpx.Response(200, json={"access_token": token, "token_type": "bearer"})
        if request.url == USER_URL:
            token = request.headers["authorization"].removeprefix("Bearer ")
            return httpx.Response(200, json=self.tokens[token])
        return httpx.Response(404)


@pytest.fixture
def github() -> FakeGithub:
    return FakeGithub()


@pytest.fixture
async def app(
    engine: AsyncEngine, migrated_database: str, github: FakeGithub
) -> AsyncIterator[FastAPI]:
    app = create_app(
        Config(
            public_url=PUBLIC_URL,
            database_url=migrated_database,
            github_client_id="client-id",
            github_client_secret="client-secret",
            session_secret="session-secret",
            admin_github_logins=frozenset({"Boss", "deputy"}),
        )
    )
    app.state.clock = FixedClock(NOW)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.MockTransport(github.handle)) as http,
    ):
        app.state.http = http
        yield app


def browser(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=PUBLIC_URL, headers={"Origin": PUBLIC_URL}
    )


async def sign_in(
    client: httpx.AsyncClient,
    github: FakeGithub,
    github_id: int,
    login: str,
    next: str | None = None,
) -> httpx.Response:
    response = await client.get(
        "/api/v1/auth/github/login", params={"next": next} if next else None
    )
    assert response.status_code == 302
    location = httpx.URL(response.headers["location"])
    profile = {"id": github_id, "login": login, "name": None, "avatar_url": None}
    code = github.authorize(location, profile)
    return await client.get(
        "/api/v1/auth/github/callback", params={"code": code, "state": location.params["state"]}
    )


async def scalar(engine: AsyncEngine, statement: Any) -> Any:
    async with create_sessionmaker(engine)() as session:
        return await session.scalar(statement)


async def user_by_login(engine: AsyncEngine, login: str) -> User:
    user = await scalar(engine, select(User).where(User.github_login == login))
    assert user is not None
    return user


async def test_github_login_creates_and_resumes_user(
    app: FastAPI, github: FakeGithub, engine: AsyncEngine
) -> None:
    async with browser(app) as client:
        response = await sign_in(client, github, 7, "octo", next="/events?tab=open")
        assert response.status_code == 302
        assert response.headers["location"] == "/events?tab=open"
        cookies = response.headers.get_list("set-cookie")
        session_cookie = next(cookie for cookie in cookies if cookie.startswith("hmm_session="))
        for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=2592000"):
            assert attribute in session_cookie
        assert any(cookie.startswith('hmm_oauth=""') for cookie in cookies)
        token = client.cookies["hmm_session"]
        stored = await scalar(engine, select(UserSession))
        assert stored.token_hash == hash_token(token)
        assert stored.expires_at == NOW + timedelta(days=30)

        me = await client.get("/api/v1/me")
        assert me.status_code == 200, me.text
        body = me.json()
        assert body["github_login"] == "octo"
        assert body["github_url"] == "https://github.com/octo"
        assert body["display_name"] == "octo"
        assert body["onboarding_status"] == OnboardingStatus.NOT_STARTED
        assert body["is_admin"] is False
        assert body["unread_count"] == 0
        assert body["telegram"]["linked"] is False
        assert body["allowed_actions"] == ["link_telegram"]

    async with browser(app) as client:
        response = await sign_in(client, github, 7, "octo-renamed")
        assert response.headers["location"] == "/onboarding"
    assert await scalar(engine, select(func.count()).select_from(User)) == 1
    user = await user_by_login(engine, "octo-renamed")
    assert user.github_id == 7
    jobs = await scalar(
        engine,
        text(
            "SELECT count(*) FROM procrastinate.procrastinate_jobs "
            "WHERE task_name = 'github_sync' AND args->>'user_id' = :user_id"
        ).bindparams(user_id=str(user.id)),
    )
    assert jobs == 2


async def test_callback_failures_redirect_to_login(
    app: FastAPI, github: FakeGithub, engine: AsyncEngine
) -> None:
    clock: FixedClock = app.state.clock
    async with browser(app) as client:
        for case in ("forged", "denied", "expired"):
            clock.set(NOW)
            login = await client.get("/api/v1/auth/github/login")
            location = httpx.URL(login.headers["location"])
            state = location.params["state"]
            code = github.authorize(location, {"id": 1, "login": "octo"})
            params = {"code": code, "state": state}
            if case == "forged":
                params["state"] = "forgéd"
            if case == "denied":
                params = {"error": "access_denied", "state": state}
            if case == "expired":
                clock.set(NOW + timedelta(minutes=11))
            response = await client.get("/api/v1/auth/github/callback", params=params)
            assert response.status_code == 302
            assert response.headers["location"] == "/login?error=bad_request", case
            assert "hmm_session" not in client.cookies

    clock.set(NOW)
    async with browser(app) as client:
        login = await client.get("/api/v1/auth/github/login", params={"next": "//evil.test"})
        location = httpx.URL(login.headers["location"])
        params = {
            "code": github.authorize(location, {"id": 1, "login": "octo"}),
            "state": location.params["state"],
        }
        response = await client.get("/api/v1/auth/github/callback", params=params)
        assert response.headers["location"] == "/onboarding"
        replay = await client.get("/api/v1/auth/github/callback", params=params)
        assert replay.headers["location"] == "/login?error=bad_request"
    assert await scalar(engine, select(func.count()).select_from(User)) == 1


async def test_admin_bootstrap_and_last_admin(
    app: FastAPI, github: FakeGithub, engine: AsyncEngine
) -> None:
    async with browser(app) as boss, browser(app) as deputy, browser(app) as member:
        await sign_in(boss, github, 1, "boss")
        await sign_in(deputy, github, 2, "Deputy")
        await sign_in(member, github, 3, "member")
        boss_user = await user_by_login(engine, "boss")
        deputy_user = await user_by_login(engine, "Deputy")
        member_user = await user_by_login(engine, "member")
        assert boss_user.is_admin and deputy_user.is_admin and not member_user.is_admin
        grants = await scalar(
            engine,
            select(func.count()).where(
                AuditLog.action == AuditAction.ADMIN_GRANT,
                AuditLog.actor_kind == ActorKind.SYSTEM,
            ),
        )
        assert grants == 2

        assert_problem(
            await member.patch(f"/api/v1/admin/users/{member_user.id}", json={"is_admin": True}),
            403,
            ErrorCode.FORBIDDEN,
        )
        response = await boss.patch(
            f"/api/v1/admin/users/{deputy_user.id}", json={"is_admin": False, "reason": "rotation"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["is_admin"] is False
        assert "grant_admin" in response.json()["allowed_actions"]
        await sign_in(deputy, github, 2, "deputy")
        assert not (await user_by_login(engine, "deputy")).is_admin

        assert_problem(
            await boss.patch(f"/api/v1/admin/users/{boss_user.id}", json={"is_admin": False}),
            409,
            ErrorCode.LAST_ADMIN,
        )
        own = await boss.patch(f"/api/v1/admin/users/{boss_user.id}", json={})
        assert "revoke_admin" not in own.json()["allowed_actions"]

        response = await boss.patch(
            f"/api/v1/admin/users/{member_user.id}",
            json={"is_admin": True, "display_name": " Member One ", "about_md": "Hi"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["display_name"] == "Member One"
        assert response.json()["is_admin"] is True
        response = await boss.patch(
            f"/api/v1/admin/users/{member_user.id}", json={"display_name": "Member One"}
        )
        assert response.status_code == 200
        edits = await scalar(
            engine, select(func.count()).where(AuditLog.action == AuditAction.USER_EDIT)
        )
        assert edits == 1
        response = await boss.patch(f"/api/v1/admin/users/{boss_user.id}", json={"is_admin": False})
        assert response.status_code == 200, response.text


async def test_logout_revokes_immediately_and_checks_origin(
    app: FastAPI, github: FakeGithub
) -> None:
    async with browser(app) as first, browser(app) as second, browser(app) as third:
        for client in (first, second, third):
            await sign_in(client, github, 1, "octo")
        for headers in ({"Origin": "https://evil.test"}, {"Origin": ""}):
            assert_problem(
                await first.post("/api/v1/auth/logout", headers=headers),
                403,
                ErrorCode.FORBIDDEN,
            )
        response = await first.post("/api/v1/auth/logout")
        assert response.status_code == 204
        assert "hmm_session" not in first.cookies
        assert (await second.get("/api/v1/me")).status_code == 200
        assert (await second.post("/api/v1/auth/logout-all")).status_code == 204
        assert_problem(await third.get("/api/v1/me"), 401, ErrorCode.UNAUTHENTICATED)
        bot = {"Authorization": "Bearer bot"}
        assert_problem(await third.get("/api/v1/me", headers=bot), 400, ErrorCode.BAD_REQUEST)
        third.cookies.clear()
        assert_problem(await third.get("/api/v1/me", headers=bot), 501, ErrorCode.NOT_IMPLEMENTED)
        assert_problem(await third.get("/api/v1/me"), 401, ErrorCode.UNAUTHENTICATED)


async def test_session_slides_and_expires(
    app: FastAPI, github: FakeGithub, engine: AsyncEngine
) -> None:
    clock: FixedClock = app.state.clock
    async with browser(app) as client:
        await sign_in(client, github, 1, "octo")
        clock.set(NOW + timedelta(minutes=4))
        response = await client.get("/api/v1/me")
        assert response.status_code == 200
        assert "set-cookie" not in response.headers
        assert (await scalar(engine, select(UserSession.last_used_at))) == NOW

        later = NOW + timedelta(minutes=6)
        clock.set(later)
        response = await client.get("/api/v1/me")
        assert "Max-Age=2592000" in response.headers["set-cookie"]
        stored = await scalar(engine, select(UserSession))
        assert stored.last_used_at == later
        assert stored.expires_at == later + timedelta(days=30)
        assert (await user_by_login(engine, "octo")).last_seen_at == later

        clock.set(later + timedelta(days=30))
        assert_problem(await client.get("/api/v1/me"), 401, ErrorCode.UNAUTHENTICATED)


async def test_ban_and_unban(app: FastAPI, github: FakeGithub, engine: AsyncEngine) -> None:
    async with browser(app) as boss, browser(app) as member:
        await sign_in(boss, github, 1, "boss")
        await sign_in(member, github, 2, "member")
        boss_user = await user_by_login(engine, "boss")
        member_user = await user_by_login(engine, "member")

        assert_problem(
            await boss.post(f"/api/v1/admin/users/{boss_user.id}/ban", json={"reason": "x"}),
            409,
            ErrorCode.INVALID_STATE,
        )
        for _ in range(2):
            response = await boss.post(
                f"/api/v1/admin/users/{member_user.id}/ban", json={"reason": " spam "}
            )
            assert response.status_code == 200, response.text
            assert response.json()["ban_reason"] == "spam"
            assert response.json()["allowed_actions"] == [
                "edit",
                "unban",
                "recompute",
                "adjust_xp",
                "override_score",
            ]
        bans = await scalar(
            engine, select(func.count()).where(AuditLog.action == AuditAction.USER_BAN)
        )
        assert bans == 1
        assert_problem(await member.get("/api/v1/me"), 401, ErrorCode.UNAUTHENTICATED)
        assert_problem(
            await boss.patch(f"/api/v1/admin/users/{member_user.id}", json={"is_admin": True}),
            409,
            ErrorCode.INVALID_STATE,
        )

        response = await sign_in(member, github, 2, "member")
        assert response.headers["location"] == "/login?error=banned"
        sessions = await scalar(
            engine,
            select(func.count()).where(UserSession.user_id == member_user.id),
        )
        assert sessions == 0

        response = await boss.post(f"/api/v1/admin/users/{member_user.id}/unban")
        assert response.status_code == 200, response.text
        assert response.json()["banned_at"] is None
        response = await sign_in(member, github, 2, "member")
        assert response.headers["location"] == "/onboarding"
        assert (await member.get("/api/v1/me")).status_code == 200


async def test_member_gate(app: FastAPI, github: FakeGithub, engine: AsyncEngine) -> None:
    async with browser(app) as client:
        await sign_in(client, github, 1, "octo")
        assert_problem(await client.get("/api/v1/areas"), 403, ErrorCode.ONBOARDING_REQUIRED)
        async with engine.begin() as connection:
            await connection.execute(
                update(User).values(onboarding_status=OnboardingStatus.REOPENED)
            )
        assert_problem(await client.get("/api/v1/areas"), 501, ErrorCode.NOT_IMPLEMENTED)
        me = await client.get("/api/v1/me")
        assert me.json()["allowed_actions"] == [
            "refresh_github",
            "link_telegram",
            "create_project",
            "create_event",
            "request_help",
        ]
