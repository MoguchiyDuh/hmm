from uuid import UUID

from hmm_contracts.common import ActorKind, AuditAction, AuditEntityType
from pydantic import JsonValue
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core.audit import record_audit
from hmm_core.db.models import Setting
from hmm_core.settings import RuntimeSettings, SettingKey, apply_setting


async def load_settings(session: AsyncSession) -> RuntimeSettings:
    rows = await session.execute(select(Setting.key, Setting.value))
    return RuntimeSettings.model_validate({key: value for key, value in rows})


async def set_setting(
    session: AsyncSession,
    key: SettingKey,
    value: JsonValue,
    actor: UUID,
    reason: str | None = None,
) -> RuntimeSettings:
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('settings'))"))
    before = await load_settings(session)
    after = apply_setting(before, key, value)
    normalized = after.model_dump(mode="json")[key.value]
    statement = insert(Setting).values(key=key.value, value=normalized, updated_by=actor)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[Setting.key],
            set_={"value": normalized, "updated_by": actor, "updated_at": text("now()")},
        )
    )
    await record_audit(
        session,
        actor=actor,
        actor_kind=ActorKind.ADMIN,
        action=AuditAction.SETTING_SET,
        entity_type=AuditEntityType.SETTING,
        entity_id=key.value,
        before=before.model_dump(mode="json")[key.value],
        after=normalized,
        reason=reason,
    )
    return after
