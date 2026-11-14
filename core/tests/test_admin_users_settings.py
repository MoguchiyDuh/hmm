from datetime import timedelta
from uuid import UUID

import pytest
from harness import Harness
from hmm_contracts.common import ActorKind, AuditAction, AuditEntityType, ErrorCode
from hmm_core import models
from hmm_core.platform.audit import record_audit
from hmm_core.platform.settings import RuntimeSettings, SettingKey, load_settings
from pydantic import JsonValue
from sqlalchemy import func, select
from test_api_skeleton import assert_problem

USERS = "/api/v1/admin/users"
AUDIT = "/api/v1/admin/audit"
SETTINGS = "/api/v1/admin/settings"


async def test_admin_users_filters_and_pagination(harness: Harness) -> None:
    h = harness
    first = await h.user(
        login="octocat",
        display_name="Alice Example",
        created_at=h.now - timedelta(days=3),
    )
    banned = await h.user(
        login="blocked-member",
        display_name="Bob Example",
        banned_at=h.now - timedelta(days=1),
        ban_reason="Moderation",
        created_at=h.now - timedelta(days=2),
    )
    actor = await h.admin(
        login="operator",
        display_name="Carol Admin",
        created_at=h.now - timedelta(days=1),
    )
    async with await h.client(actor) as client:
        response = await client.get(USERS)
        assert response.status_code == 200, response.text
        page = response.json()
        assert [item["id"] for item in page["items"]] == [
            str(actor.id),
            str(banned.id),
            str(first.id),
        ]
        assert page["next_cursor"] is None
        assert "unban" in page["items"][1]["allowed_actions"]
        for params, expected in [
            ({"q": "aLiCe"}, [first.id]),
            ({"q": "OcToCaT"}, [first.id]),
            ({"q": ""}, [actor.id, banned.id, first.id]),
            ({"q": "missing"}, []),
            ({"banned": "true"}, [banned.id]),
            ({"banned": "false"}, [actor.id, first.id]),
            ({"admin": "true"}, [actor.id]),
            ({"admin": "false"}, [banned.id, first.id]),
            ({"q": "Example", "banned": "false", "admin": "false"}, [first.id]),
        ]:
            response = await client.get(USERS, params=params)
            assert response.status_code == 200, response.text
            assert [item["id"] for item in response.json()["items"]] == [
                str(user_id) for user_id in expected
            ]
        response = await client.get(USERS, params={"limit": 1})
        assert response.status_code == 200, response.text
        page = response.json()
        assert [item["id"] for item in page["items"]] == [str(actor.id)]
        assert page["next_cursor"] is not None
        response = await client.get(USERS, params={"cursor": page["next_cursor"]})
        assert response.status_code == 200, response.text
        page = response.json()
        assert [item["id"] for item in page["items"]] == [str(banned.id), str(first.id)]
        assert page["next_cursor"] is None
        assert_problem(
            await client.get(USERS, params={"cursor": "malformed"}), 400, ErrorCode.BAD_REQUEST
        )


async def test_admin_users_cursor_ties(harness: Harness) -> None:
    h = harness
    actor = await h.admin(created_at=h.now)
    users = [actor, await h.user(created_at=h.now), await h.user(created_at=h.now)]
    expected = [str(user.id) for user in sorted(users, key=lambda user: user.id, reverse=True)]
    ids: list[str] = []
    params: dict[str, str | int] = {"limit": 1}
    async with await h.client(actor) as client:
        for _ in users:
            response = await client.get(USERS, params=params)
            assert response.status_code == 200, response.text
            page = response.json()
            ids.extend(item["id"] for item in page["items"])
            if page["next_cursor"] is None:
                break
            params["cursor"] = page["next_cursor"]
    assert ids == expected
    assert page["next_cursor"] is None


async def test_admin_audit_setting_and_filters(harness: Harness) -> None:
    h = harness
    actor = await h.admin()
    other = await h.admin()
    target = await h.user()
    async with await h.client(actor) as client:
        response = await client.put(
            f"{SETTINGS}/review_hours", json={"value": 60, "reason": "tune"}
        )
        assert response.status_code == 200, response.text
        response = await client.get(AUDIT)
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["next_cursor"] is None
        assert len(page["items"]) == 1
        setting_entry = page["items"][0]
        assert setting_entry["actor_id"] == str(actor.id)
        assert setting_entry["actor_kind"] == "admin"
        assert setting_entry["action"] == "setting_set"
        assert setting_entry["entity_type"] == "setting"
        assert setting_entry["entity_id"] == "review_hours"
        assert setting_entry["before"] == 48
        assert setting_entry["after"] == 60
        assert setting_entry["reason"] == "tune"
        async with h.db() as session:
            for entry_actor in [actor, other]:
                await record_audit(
                    session,
                    actor=entry_actor.id,
                    actor_kind=ActorKind.ADMIN,
                    action=AuditAction.USER_BAN,
                    entity_type=AuditEntityType.USER,
                    entity_id=target.id,
                    reason="Moderation",
                )
        response = await client.get(AUDIT)
        assert response.status_code == 200, response.text
        entries = response.json()["items"]
        assert [entry["actor_id"] for entry in entries] == [
            str(other.id),
            str(actor.id),
            str(actor.id),
        ]
        assert entries[-1] == setting_entry
        for params, expected in [
            ({"actor": str(actor.id)}, entries[1:]),
            ({"entity_type": "setting"}, [setting_entry]),
            ({"entity_id": str(target.id)}, entries[:2]),
            (
                {
                    "actor": str(actor.id),
                    "entity_type": "user",
                    "entity_id": str(target.id),
                },
                [entries[1]],
            ),
            ({"actor": str(UUID(int=1))}, []),
            ({"entity_type": "project"}, []),
            ({"entity_id": str(UUID(int=1))}, []),
        ]:
            response = await client.get(AUDIT, params=params)
            assert response.status_code == 200, response.text
            assert response.json() == {"items": expected, "next_cursor": None}
        response = await client.get(AUDIT, params={"limit": 1})
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["items"] == entries[:1]
        assert page["next_cursor"] is not None
        response = await client.get(AUDIT, params={"cursor": page["next_cursor"]})
        assert response.status_code == 200, response.text
        assert response.json() == {"items": entries[1:], "next_cursor": None}
        assert_problem(
            await client.get(AUDIT, params={"cursor": "malformed"}), 400, ErrorCode.BAD_REQUEST
        )
        assert_problem(
            await client.get(AUDIT, params={"entity_id": "review_hours"}),
            422,
            ErrorCode.VALIDATION_ERROR,
        )


async def test_admin_settings_list_and_update(harness: Harness) -> None:
    h = harness
    actor = await h.admin()
    defaults = RuntimeSettings().model_dump(mode="json")
    async with await h.client(actor) as client:
        response = await client.get(SETTINGS)
        assert response.status_code == 200, response.text
        settings = response.json()
        assert [item["key"] for item in settings] == [key.value for key in SettingKey]
        for item in settings:
            assert item["value"] == defaults[item["key"]]
            assert item["default"] == defaults[item["key"]]
            assert item["description"]
            assert isinstance(item["value_schema"], dict)
            assert item["updated_by"] is None
            assert item["updated_at"] is None
            assert item["allowed_actions"] == ["edit"]
        for value in [60, 72]:
            response = await client.put(
                f"{SETTINGS}/review_hours", json={"value": value, "reason": "tune"}
            )
            assert response.status_code == 200, response.text
            setting = response.json()
            assert setting["key"] == "review_hours"
            assert setting["value"] == value
            assert setting["default"] == 48
            assert setting["description"]
            assert setting["value_schema"] == {
                "exclusiveMinimum": 0,
                "type": "integer",
            }
            assert setting["updated_by"] == str(actor.id)
            assert setting["updated_at"] is not None
            assert setting["allowed_actions"] == ["edit"]
            response = await client.get(SETTINGS)
            assert response.status_code == 200, response.text
            assert (
                next(item for item in response.json() if item["key"] == "review_hours") == setting
            )
    async with h.db() as session:
        rows = list(await session.scalars(select(models.Setting)))
        assert len(rows) == 1
        assert rows[0].value == 72
        assert rows[0].updated_by == actor.id
        audit = list(
            await session.scalars(select(models.AuditLog).order_by(models.AuditLog.created_at))
        )
        assert [(entry.before, entry.after, entry.reason) for entry in audit] == [
            (48, 60, "tune"),
            (60, 72, "tune"),
        ]


@pytest.mark.parametrize(
    "key,value",
    [
        ("review_hours", "not-an-int"),
        ("review_hours", True),
        ("review_hours", 60.0),
        ("review_hours", 0),
        ("review_min_hours", 72),
        ("unknown", 60),
    ],
)
async def test_admin_settings_invalid_values(harness: Harness, key: str, value: JsonValue) -> None:
    h = harness
    actor = await h.admin()
    async with await h.client(actor) as client:
        response = await client.put(f"{SETTINGS}/{key}", json={"value": value})
        assert_problem(response, 422, ErrorCode.VALIDATION_ERROR)
    async with h.db() as session:
        assert await load_settings(session) == RuntimeSettings()
        assert await session.scalar(select(func.count()).select_from(models.Setting)) == 0
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 0


async def test_admin_settings_invalid_update_preserves_stored_values(harness: Harness) -> None:
    h = harness
    actor = await h.admin()
    async with await h.client(actor) as client:
        response = await client.put(f"{SETTINGS}/review_hours", json={"value": 60})
        assert response.status_code == 200, response.text
        stored = response.json()
        assert_problem(
            await client.put(f"{SETTINGS}/review_min_hours", json={"value": 72}),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
        assert_problem(
            await client.put(f"{SETTINGS}/review_hours", json={"value": 1}),
            422,
            ErrorCode.VALIDATION_ERROR,
        )
        response = await client.get(SETTINGS)
        assert response.status_code == 200, response.text
        assert next(item for item in response.json() if item["key"] == "review_hours") == stored
    async with h.db() as session:
        assert (await load_settings(session)).review_hours == 60
        assert await session.scalar(select(func.count()).select_from(models.Setting)) == 1
        assert await session.scalar(select(func.count()).select_from(models.AuditLog)) == 1


async def test_admin_settings_normalizes_numeric_value(harness: Harness) -> None:
    h = harness
    actor = await h.admin()
    async with await h.client(actor) as client:
        response = await client.put(f"{SETTINGS}/mentor_min_score", json={"value": 7})
        assert response.status_code == 200, response.text
        assert response.json()["value"] == 7.0
        assert type(response.json()["value"]) is float
    async with h.db() as session:
        row = await session.get(models.Setting, "mentor_min_score")
        assert row is not None
        assert row.value == 7.0
        assert type(row.value) is float
        entry = (await session.scalars(select(models.AuditLog))).one()
        assert entry.before == 6.0
        assert entry.after == 7.0
        assert type(entry.after) is float
        assert entry.reason is None


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", USERS),
        ("GET", AUDIT),
        ("GET", SETTINGS),
        ("PUT", f"{SETTINGS}/review_hours"),
    ],
)
async def test_admin_users_settings_access(harness: Harness, method: str, path: str) -> None:
    h = harness
    member = await h.user()
    actor = await h.admin()
    telegram_id = await h.link_telegram(actor)
    body = {"value": 60} if method == "PUT" else None
    async with await h.client(member) as client:
        assert_problem(await client.request(method, path, json=body), 403, ErrorCode.FORBIDDEN)
    async with h.anonymous() as client:
        assert_problem(
            await client.request(method, path, json=body), 401, ErrorCode.UNAUTHENTICATED
        )
    async with h.bot(telegram_id) as client:
        assert_problem(
            await client.request(method, path, json=body), 401, ErrorCode.UNAUTHENTICATED
        )
