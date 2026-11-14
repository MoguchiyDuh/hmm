import asyncio
import hmac
import re
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import FastAPI
from hmm_contracts.common import ActorKind, AuditAction, ErrorCode, OnboardingStatus
from hmm_core.app import create_app
from hmm_core.identity.github import TOKEN_URL, USER_URL
from hmm_core.identity.service import hash_token, pkce_challenge, require_service_token
from hmm_core.models import AuditLog, TelegramLink, TelegramLinkCode, User, UserSession
from hmm_core.platform.clock import FixedClock
from hmm_core.platform.config import Config
from hmm_core.platform.db import create_sessionmaker
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
            bot_service_token="bot-token",
            parser_service_token="parser-token",
            telegram_bot_username="hmm_test_bot",
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
    assert jobs == 1


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
        assert_problem(await third.get("/api/v1/me", headers=bot), 401, ErrorCode.UNAUTHENTICATED)
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


def bot_headers(telegram_user_id: int | None = None) -> dict[str, str]:
    headers = {"Authorization": "Bearer bot-token"}
    if telegram_user_id is not None:
        headers["X-Telegram-User-Id"] = str(telegram_user_id)
    return headers


async def link_account(
    web: httpx.AsyncClient, bot: httpx.AsyncClient, telegram_user_id: int = 123
) -> httpx.Response:
    response = await web.post("/api/v1/me/telegram/link-code")
    assert response.status_code == 201, response.text
    return await bot.post(
        "/api/v1/internal/telegram/link",
        headers=bot_headers(),
        json={
            "code": response.json()["code"],
            "telegram_user_id": telegram_user_id,
            "chat_id": telegram_user_id,
            "username": "octo_tg",
        },
    )


def test_service_config_from_env() -> None:
    config = Config.from_env(
        {
            "BOT_SERVICE_TOKEN": "bot-secret",
            "PARSER_SERVICE_TOKEN": "parser-secret",
            "TELEGRAM_BOT_USERNAME": "hmm_bot",
        }
    )
    assert config.bot_service_token == "bot-secret"
    assert config.parser_service_token == "parser-secret"
    assert config.telegram_bot_username == "hmm_bot"
    for env in ({}, {"BOT_SERVICE_TOKEN": "", "PARSER_SERVICE_TOKEN": ""}):
        config = Config.from_env(env)
        assert config.bot_service_token is None
        assert config.parser_service_token is None


@pytest.mark.parametrize(
    ("path", "valid_token", "other_token"),
    [
        ("/api/v1/internal/notifications/pending", "bot-token", "parser-token"),
        ("/api/v1/internal/areas", "parser-token", "bot-token"),
    ],
)
async def test_service_tokens_are_scoped(
    app: FastAPI, path: str, valid_token: str, other_token: str
) -> None:
    async with browser(app) as client:
        for authorization in (
            None,
            "Bearer wrong",
            f"Bearer {other_token}",
            "Basic secret",
            "Bearer",
        ):
            headers = {"Authorization": authorization} if authorization else {}
            assert_problem(await client.get(path, headers=headers), 401, ErrorCode.UNAUTHENTICATED)
        assert_problem(
            await client.get(path, headers={"Authorization": f"Bearer {valid_token}"}),
            501,
            ErrorCode.NOT_IMPLEMENTED,
        )


@pytest.mark.parametrize("configured", [None, ""])
async def test_unconfigured_service_tokens_fail(app: FastAPI, configured: str | None) -> None:
    config: Config = app.state.config
    app.state.config = config.model_copy(
        update={"bot_service_token": configured, "parser_service_token": configured}
    )
    async with browser(app) as client:
        for path, token in (
            ("/api/v1/internal/notifications/pending", "bot-token"),
            ("/api/v1/internal/areas", "parser-token"),
            ("/api/v1/me", "bot-token"),
        ):
            assert_problem(
                await client.get(path, headers={"Authorization": f"Bearer {token}"}),
                401,
                ErrorCode.UNAUTHENTICATED,
            )


async def test_service_token_comparisons_use_hmac(app: FastAPI) -> None:
    async with browser(app) as client:
        with patch(
            "hmm_core.identity.service.hmac.compare_digest", wraps=hmac.compare_digest
        ) as compare:
            for path, configured in (
                ("/api/v1/internal/notifications/pending", "bot-token"),
                ("/api/v1/internal/areas", "parser-token"),
                ("/api/v1/me", "bot-token"),
            ):
                for token in (configured, "wrong"):
                    await client.get(path, headers={"Authorization": f"Bearer {token}"})
                    compare.assert_called_with(token.encode(), configured.encode())
            assert compare.call_count == 6
            require_service_token("секрет", "секрет")
            compare.assert_called_with("секрет".encode(), "секрет".encode())


@pytest.mark.parametrize(
    "telegram_user_id", [None, "999", "invalid", str(2**63), str(-(2**63) - 1)]
)
async def test_bot_unlinked_or_missing_identity(app: FastAPI, telegram_user_id: str | None) -> None:
    headers = bot_headers()
    if telegram_user_id is not None:
        headers["X-Telegram-User-Id"] = telegram_user_id
    async with browser(app) as client:
        assert_problem(
            await client.get("/api/v1/me", headers=headers), 401, ErrorCode.TELEGRAM_NOT_LINKED
        )
        headers["Authorization"] = "Bearer parser-token"
        assert_problem(
            await client.get("/api/v1/me", headers=headers), 401, ErrorCode.UNAUTHENTICATED
        )


async def test_bot_resolves_user_and_clears_broken_link(
    app: FastAPI, github: FakeGithub, engine: AsyncEngine
) -> None:
    async with browser(app) as web, browser(app) as bot:
        await sign_in(web, github, 1, "boss")
        linked = await link_account(web, bot)
        assert linked.status_code == 200, linked.text
        actor = await user_by_login(engine, "boss")
        assert linked.json() == {"user_id": str(actor.id), "display_name": "boss"}
        async with engine.begin() as connection:
            await connection.execute(update(TelegramLink).values(broken_at=NOW))
        bot.headers.pop("Origin")
        response = await bot.get("/api/v1/me", headers=bot_headers(123))
        assert response.status_code == 200, response.text
        assert response.json()["id"] == str(actor.id)
        assert response.json()["telegram"]["broken_at"] is None
        assert "unlink_telegram" in response.json()["allowed_actions"]
        assert (await scalar(engine, select(TelegramLink))).broken_at is None
        assert_problem(
            await bot.get("/api/v1/areas", headers=bot_headers(123)),
            403,
            ErrorCode.ONBOARDING_REQUIRED,
        )
        assert_problem(
            await bot.get("/api/v1/admin/users", headers=bot_headers(123)),
            401,
            ErrorCode.UNAUTHENTICATED,
        )
        async with engine.begin() as connection:
            await connection.execute(
                update(User).values(onboarding_status=OnboardingStatus.COMPLETED)
            )
        assert_problem(
            await bot.get("/api/v1/areas", headers=bot_headers(123)), 501, ErrorCode.NOT_IMPLEMENTED
        )
        response = await bot.post("/api/v1/me/telegram/link-code", headers=bot_headers(123))
        assert response.status_code == 201, response.text
        async with engine.begin() as connection:
            await connection.execute(update(User).values(banned_at=NOW))
            await connection.execute(update(TelegramLink).values(broken_at=NOW))
        assert_problem(await bot.get("/api/v1/me", headers=bot_headers(123)), 403, ErrorCode.BANNED)
        assert (await scalar(engine, select(TelegramLink))).broken_at == NOW


@pytest.mark.parametrize(
    "headers",
    [
        bot_headers(123),
        bot_headers(),
        {"X-Telegram-User-Id": "123"},
        {"Authorization": "Basic bad"},
    ],
)
async def test_cookie_and_bot_credentials_are_rejected(
    app: FastAPI, github: FakeGithub, headers: dict[str, str]
) -> None:
    async with browser(app) as web:
        await sign_in(web, github, 1, "boss")
        for path in ("/api/v1/me", "/api/v1/admin/users"):
            assert_problem(await web.get(path, headers=headers), 400, ErrorCode.BAD_REQUEST)


async def test_link_codes_format_expiry_and_replacement(
    app: FastAPI, github: FakeGithub, engine: AsyncEngine
) -> None:
    async with browser(app) as first, browser(app) as second, browser(app) as bot:
        await sign_in(first, github, 1, "first")
        await sign_in(second, github, 2, "second")
        second_code = (await second.post("/api/v1/me/telegram/link-code")).json()["code"]
        codes: list[str] = []
        for _ in range(2):
            response = await first.post("/api/v1/me/telegram/link-code")
            assert response.status_code == 201, response.text
            body = response.json()
            assert re.fullmatch("[A-Z2-7]{8}", body["code"])
            assert body["deep_link"] == f"https://t.me/hmm_test_bot?start={body['code']}"
            assert datetime.fromisoformat(body["expires_at"]) == NOW + timedelta(minutes=10)
            codes.append(body["code"])
        assert codes[0] != codes[1]
        assert (
            await scalar(
                engine, select(TelegramLinkCode).where(TelegramLinkCode.code == second_code)
            )
        ).expires_at == NOW + timedelta(minutes=10)
        for code, status, error in (
            ("MISSING", 404, ErrorCode.CODE_INVALID),
            (codes[0], 410, ErrorCode.CODE_EXPIRED),
        ):
            response = await bot.post(
                "/api/v1/internal/telegram/link",
                headers=bot_headers(),
                json={"code": code, "telegram_user_id": 123, "chat_id": 123, "username": None},
            )
            assert_problem(response, status, error)
        clock: FixedClock = app.state.clock
        clock.set(NOW + timedelta(minutes=10))
        response = await bot.post(
            "/api/v1/internal/telegram/link",
            headers=bot_headers(),
            json={"code": codes[1], "telegram_user_id": 123, "chat_id": 123, "username": None},
        )
        assert_problem(response, 410, ErrorCode.CODE_EXPIRED)
        assert await scalar(engine, select(func.count()).select_from(TelegramLink)) == 0


async def test_link_replacement_conflict_and_replay(
    app: FastAPI, github: FakeGithub, engine: AsyncEngine
) -> None:
    async with browser(app) as first, browser(app) as second, browser(app) as bot:
        await sign_in(first, github, 1, "first")
        await sign_in(second, github, 2, "second")
        assert (await link_account(first, bot, 123)).status_code == 200
        assert_problem(await link_account(second, bot, 123), 409, ErrorCode.TELEGRAM_ALREADY_LINKED)
        second_code = await scalar(
            engine, select(TelegramLinkCode).where(TelegramLinkCode.used_at.is_(None))
        )
        assert second_code.used_at is None
        assert (await link_account(second, bot, 456)).status_code == 200
        async with engine.begin() as connection:
            await connection.execute(update(TelegramLink).values(broken_at=NOW))
        clock: FixedClock = app.state.clock
        later = NOW + timedelta(minutes=1)
        clock.set(later)
        assert (await link_account(first, bot, 789)).status_code == 200
        first_user = await user_by_login(engine, "first")
        link = await scalar(
            engine, select(TelegramLink).where(TelegramLink.user_id == first_user.id)
        )
        assert link.telegram_user_id == 789
        assert link.chat_id == 789 and link.username == "octo_tg"
        assert link.linked_at == later and link.broken_at is None
        assert (await first.get("/api/v1/me")).json()["telegram"]["username"] == "octo_tg"
        assert_problem(
            await bot.get("/api/v1/me", headers=bot_headers(123)),
            401,
            ErrorCode.TELEGRAM_NOT_LINKED,
        )
        used = await scalar(
            engine, select(TelegramLinkCode).where(TelegramLinkCode.used_at == later)
        )
        replay = await bot.post(
            "/api/v1/internal/telegram/link",
            headers=bot_headers(),
            json={"code": used.code, "telegram_user_id": 1000, "chat_id": 1000, "username": None},
        )
        assert_problem(replay, 410, ErrorCode.CODE_EXPIRED)
        async with engine.begin() as connection:
            await connection.execute(
                update(TelegramLink)
                .where(TelegramLink.user_id == first_user.id)
                .values(broken_at=NOW)
            )
        assert (await link_account(first, bot, 789)).status_code == 200
        assert (
            await scalar(
                engine, select(TelegramLink.broken_at).where(TelegramLink.user_id == first_user.id)
            )
            is None
        )
        assert await scalar(engine, select(func.count()).select_from(TelegramLink)) == 2
        assert await scalar(engine, select(func.count()).select_from(AuditLog)) == 0


async def test_unlink_is_idempotent_from_web_and_bot(
    app: FastAPI, github: FakeGithub, engine: AsyncEngine
) -> None:
    async with browser(app) as web, browser(app) as bot:
        await sign_in(web, github, 1, "octo")
        for _ in range(2):
            assert (await web.delete("/api/v1/me/telegram")).status_code == 204
        assert (await link_account(web, bot)).status_code == 200
        for _ in range(2):
            response = await bot.post(
                "/api/v1/internal/telegram/unlink",
                headers=bot_headers(),
                json={"telegram_user_id": 123},
            )
            assert response.status_code == 204, response.text
        assert_problem(
            await bot.get("/api/v1/me", headers=bot_headers(123)),
            401,
            ErrorCode.TELEGRAM_NOT_LINKED,
        )
        assert (await link_account(web, bot)).status_code == 200
        for _ in range(2):
            assert (await web.delete("/api/v1/me/telegram")).status_code == 204
        assert (await web.get("/api/v1/me")).json()["telegram"]["linked"] is False
        assert await scalar(engine, select(func.count()).select_from(TelegramLink)) == 0
        assert await scalar(engine, select(func.count()).select_from(AuditLog)) == 0


async def test_concurrent_code_creation_leaves_one_active(
    app: FastAPI, github: FakeGithub, engine: AsyncEngine
) -> None:
    async with browser(app) as web:
        await sign_in(web, github, 1, "octo")
        responses = await asyncio.gather(
            *(web.post("/api/v1/me/telegram/link-code") for _ in range(4))
        )
        assert all(response.status_code == 201 for response in responses)
        assert (
            await scalar(
                engine,
                select(func.count()).where(
                    TelegramLinkCode.used_at.is_(None), TelegramLinkCode.expires_at > NOW
                ),
            )
            == 1
        )


async def test_concurrent_code_use_succeeds_once(
    app: FastAPI, github: FakeGithub, engine: AsyncEngine
) -> None:
    async with browser(app) as web, browser(app) as bot:
        await sign_in(web, github, 1, "octo")
        code = (await web.post("/api/v1/me/telegram/link-code")).json()["code"]
        responses = await asyncio.gather(
            *(
                bot.post(
                    "/api/v1/internal/telegram/link",
                    headers=bot_headers(),
                    json={
                        "code": code,
                        "telegram_user_id": identity,
                        "chat_id": identity,
                        "username": None,
                    },
                )
                for identity in (123, 456)
            )
        )
        assert sorted(response.status_code for response in responses) == [200, 410]
        assert await scalar(engine, select(func.count()).select_from(TelegramLink)) == 1


async def test_concurrent_telegram_claim_has_one_owner(
    app: FastAPI, github: FakeGithub, engine: AsyncEngine
) -> None:
    async with browser(app) as first, browser(app) as second, browser(app) as bot:
        await sign_in(first, github, 1, "first")
        await sign_in(second, github, 2, "second")
        responses = await asyncio.gather(link_account(first, bot), link_account(second, bot))
        assert sorted(response.status_code for response in responses) == [200, 409]
        assert await scalar(engine, select(func.count()).select_from(TelegramLink)) == 1
