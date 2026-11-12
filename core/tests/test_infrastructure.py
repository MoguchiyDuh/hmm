from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import httpx
import pytest
from hmm_contracts.common import ErrorCode
from hmm_core.api.errors import ProblemError
from hmm_core.api.pagination import decode_cursor, encode_cursor
from hmm_core.app import create_app
from hmm_core.clock import CurrentClock, FixedClock, SystemClock
from hmm_core.config import Config
from hmm_core.db.models import User
from hmm_core.jobs import RETRY_POLICY
from procrastinate.jobs import Job


def test_cursor_roundtrip() -> None:
    columns = [User.created_at, User.github_id, User.display_name, User.id]
    values = [datetime.now(UTC), 42, 'name, "quoted"', UUID(int=1)]
    assert decode_cursor(encode_cursor(values), columns) == values


@pytest.mark.parametrize(
    "cursor", ["", "!!", "e30", "W10", "bnVsbA", encode_cursor(["x", "y"]), encode_cursor(["x"])]
)
def test_malformed_cursor(cursor: str) -> None:
    with pytest.raises(ProblemError) as error:
        decode_cursor(cursor, [User.created_at, User.id])
    assert error.value.code == ErrorCode.BAD_REQUEST


async def test_clock_injected_into_route() -> None:
    app = create_app()
    stamp = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=3)))
    clock = FixedClock(stamp)
    app.state.clock = clock

    @app.get("/now")
    async def now(clock: CurrentClock) -> dict[str, datetime]:
        return {"now": clock.now()}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/now")).json() == {"now": "2025-12-31T21:00:00Z"}
        clock.set(datetime(2026, 2, 1, tzinfo=UTC))
        assert (await client.get("/now")).json() == {"now": "2026-02-01T00:00:00Z"}
    assert SystemClock().now().tzinfo == UTC
    with pytest.raises(ValueError):
        clock.set(datetime(2026, 1, 1))


async def test_unreachable_readiness() -> None:
    app = create_app(
        Config(
            public_url="http://test", database_url="postgresql+psycopg://hmm:hmm@127.0.0.1:1/hmm"
        )
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}
        assert (await client.get("/healthz")).status_code == 200


def test_retry_policy() -> None:
    for attempts in range(6):
        job = Job(
            task_name="test",
            task_kwargs={},
            attempts=attempts,
            queue="default",
            lock=None,
            queueing_lock=None,
        )
        now = datetime.now(UTC)
        decision = RETRY_POLICY.get_retry_decision(exception=RuntimeError(), job=job)
        if attempts == 5:
            assert decision is None
        else:
            assert decision is not None
            assert decision.retry_at is not None
            delay = (decision.retry_at - now).total_seconds()
            assert delay == pytest.approx(2 ** (attempts + 1), abs=0.5)
