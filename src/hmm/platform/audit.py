from uuid import UUID

from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from hmm import models
from hmm.common import ActorKind, AuditAction, AuditEntityType


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
