from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field

from hmm.catalog.import_schema import EventImport
from hmm.common import Contract, EventFormat, EventKind
from hmm.parser.base import Adapter, Candidate, SourceContext, adapter
from hmm.parser.fetch import FetchError
from hmm.parser.normalize import (
    CURRENCIES,
    as_dict,
    clean_text,
    guess_kind,
    map_areas,
    money,
    parse_datetime,
    slug_terms,
    text_terms,
)

OFFICIAL_PAGE = 20
INTERNAL_PAGE = 100
NEVER_YEAR = 2100


@dataclass(frozen=True, slots=True, kw_only=True)
class Competition:
    id: int
    slug: str
    title: str
    summary: str | None
    organizer: str | None
    category: str | None
    amount: float | None
    currency: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    entrant_deadline: datetime | None
    merger_deadline: datetime | None
    team_max: int | None
    tags: list[str]


class KaggleOptions(Contract):
    token_env: str = "KAGGLE_API_TOKEN"
    api: Annotated[str, Field(pattern=r"^https://")] = (
        "https://api.kaggle.com/v1/competitions.CompetitionApiService/ListCompetitions"
    )
    internal: Annotated[str, Field(pattern=r"^https://")] = (
        "https://www.kaggle.com/api/i/competitions.CompetitionService/ListCompetitions"
    )
    site: Annotated[str, Field(pattern=r"^https://")] = "https://www.kaggle.com/competitions"
    min_reward: Annotated[float, Field(ge=0)] = 1000
    community_pages: Annotated[int, Field(ge=0)] = 6
    max_pages: Annotated[int, Field(ge=1)] = 6
    skip_categories: tuple[str, ...] = ("Getting Started",)


def tag_names(values: Any) -> list[str]:
    return [
        str(item["name"]).strip()
        for item in values or []
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip()
    ]


def official(row: dict[str, Any]) -> Competition | None:
    url = str(row.get("url") or row.get("ref") or "")
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    title = clean_text(row.get("title"))
    if not isinstance(row.get("id"), int) or not slug or title is None:
        return None
    amount, currency = money(str(row.get("reward") or ""))
    team = row.get("maxTeamSize")
    return Competition(
        id=row["id"],
        slug=slug,
        title=title,
        summary=clean_text(row.get("description")),
        organizer=clean_text(row.get("organizationName") or row.get("hostName")),
        category=clean_text(row.get("category")),
        amount=amount,
        currency=currency,
        starts_at=parse_datetime(row.get("enabledDate")),
        ends_at=parse_datetime(row.get("deadline")),
        entrant_deadline=parse_datetime(row.get("newEntrantDeadline")),
        merger_deadline=parse_datetime(row.get("mergerDeadline")),
        team_max=team if isinstance(team, int) and team >= 1 else None,
        tags=tag_names(row.get("tags")),
    )


def internal(row: dict[str, Any]) -> Competition | None:
    slug = row.get("competitionName")
    title = clean_text(row.get("title"))
    if not isinstance(row.get("id"), int) or not isinstance(slug, str) or title is None:
        return None
    reward = as_dict(row.get("reward"))
    code = str(reward.get("id") or "").upper()
    quantity = reward.get("quantity")
    cash = code in CURRENCIES and isinstance(quantity, (int, float)) and quantity > 0
    organization = as_dict(row.get("organization"))
    team = row.get("maxTeamSize")
    return Competition(
        id=row["id"],
        slug=slug,
        title=title,
        summary=clean_text(row.get("briefDescription")),
        organizer=clean_text(organization.get("name") or row.get("hostName")),
        category=None,
        amount=float(quantity) if cash else None,
        currency=code if cash else None,
        starts_at=parse_datetime(row.get("dateEnabled")),
        ends_at=parse_datetime(row.get("deadline")),
        entrant_deadline=parse_datetime(row.get("prohibitNewEntrantsExplicitDeadline")),
        merger_deadline=parse_datetime(row.get("teamMergerExplicitDeadline")),
        team_max=team if isinstance(team, int) and team >= 1 else None,
        tags=tag_names(row.get("categories")),
    )


def rows_of(data: Any, url: str) -> list[dict[str, Any]]:
    rows = data.get("competitions") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise FetchError(url, "kaggle: expected a competitions list")
    return [row for row in rows if isinstance(row, dict)]


@adapter
class Kaggle(Adapter[KaggleOptions]):
    name = "kaggle"
    options_model = KaggleOptions

    def warnings(self, ctx: SourceContext) -> list[str]:
        if ctx.env.get(self.options.token_env):
            return []
        return [
            f"{self.options.token_env} is not set: using Kaggle's anonymous internal API, "
            "which skips community competitions"
        ]

    def paying(self, competition: Competition) -> bool:
        return competition.amount is not None and competition.amount >= self.options.min_reward

    async def discover(self, ctx: SourceContext) -> AsyncIterator[Candidate]:
        token = ctx.env.get(self.options.token_env)
        found = self.official(ctx, token) if token else self.internal(ctx)
        seen: set[int] = set()
        async for competition in found:
            if competition.id not in seen:
                seen.add(competition.id)
                yield Candidate(
                    key=str(competition.id),
                    url=f"{self.options.site.rstrip('/')}/{competition.slug}",
                    payload=competition,
                )

    async def official(self, ctx: SourceContext, token: str) -> AsyncIterator[Competition]:
        headers = {"Authorization": f"Bearer {token}"}
        body: dict[str, Any] = {"group": "COMPETITION_LIST_TAB_GENERAL", "pageSize": OFFICIAL_PAGE}
        for _ in range(self.options.max_pages):
            response = await ctx.post(self.options.api, body, headers=headers)
            data = response.json()
            for row in rows_of(data, response.url):
                if (competition := official(row)) is not None:
                    yield competition
            following = data.get("nextPageToken")
            if not following:
                break
            body = {**body, "pageToken": following}
        for page in range(1, self.options.community_pages + 1):
            response = await ctx.post(
                self.options.api,
                {
                    "group": "COMPETITION_LIST_TAB_COMMUNITY",
                    "sortBy": "COMPETITION_SORT_BY_PRIZE",
                    "pageSize": OFFICIAL_PAGE,
                    "page": page,
                },
                headers=headers,
            )
            batch = [
                item for row in rows_of(response.json(), response.url) if (item := official(row))
            ]
            for competition in batch:
                yield competition
            if len(batch) < OFFICIAL_PAGE or not self.paying(batch[-1]):
                return

    async def internal(self, ctx: SourceContext) -> AsyncIterator[Competition]:
        token = ""
        for _ in range(self.options.max_pages):
            body: dict[str, Any] = {"pageSize": INTERNAL_PAGE, "listOption": "LIST_OPTION_ACTIVE"}
            if token:
                body["pageToken"] = token
            response = await ctx.post(self.options.internal, body)
            data = response.json()
            batch = [item for row in rows_of(data, response.url) if (item := internal(row))]
            for competition in batch:
                yield competition
            token = str(data.get("nextPageToken") or "")
            if not token or not batch or not self.paying(batch[-1]):
                return

    async def extract(self, ctx: SourceContext, candidate: Candidate) -> list[EventImport]:
        competition: Competition = candidate.payload
        if not self.paying(competition) or competition.category in self.options.skip_categories:
            return []
        starts_at, ends_at = competition.starts_at, competition.ends_at
        if (
            starts_at is None
            or ends_at is None
            or ends_at < max(starts_at, ctx.cutoff)
            or ends_at.year >= NEVER_YEAR
        ):
            return []
        if not ctx.in_scope(None, EventFormat.ONLINE):
            return []
        deadline = competition.entrant_deadline or competition.merger_deadline or ends_at
        title = competition.title
        fields: dict[str, Any] = {
            "kind": guess_kind(title, [], EventKind.CONTEST),
            "title": title,
            "summary": competition.summary,
            "organizer": competition.organizer,
            "website_url": candidate.url,
            "registration_url": candidate.url,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "registration_deadline": min(deadline, ends_at),
            "format": EventFormat.ONLINE,
            "themes": ["Kaggle", *competition.tags][:10],
            "areas": map_areas(
                [*slug_terms(competition.tags), *text_terms(title)], ctx.areas, primary="ml_ai"
            ),
            "team_size_max": competition.team_max,
            "prize": {"amount": competition.amount, "currency": competition.currency},
            "confidence": ctx.confidence(0.85),
        }
        return [self.build(ctx, candidate.key, [candidate.url], fields)]
