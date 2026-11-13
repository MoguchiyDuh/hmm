from uuid import UUID

from hmm_contracts.common import ActorKind, AuditAction, AuditEntityType
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from hmm_core import models


async def record_audit(
    session: AsyncSession,
    *,
    actor: UUID | None,
    actor_kind: ActorKind,
    action: AuditAction,
    entity_type: AuditEntityType,
    entity_id: UUID | str,
    before: JsonValue = None,
    after: JsonValue = None,
    reason: str | None = None,
) -> None:
    session.add(
        models.AuditLog(
            actor_id=actor,
            actor_kind=actor_kind,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            before=before,
            after=after,
            reason=reason,
        )
    )
    await session.flush()
