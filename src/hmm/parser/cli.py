import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import httpx
from pydantic import SecretStr

from hmm.algo.models import LLMConfig
from hmm.common import SEED_AREA_KEYS
from hmm.parser.config import SourceConfig, SourceConfigError, load_sources
from hmm.parser.fetch import Fetcher
from hmm.parser.llm import MemoryCache, Refiner
from hmm.parser.pipeline import collect, dedupe, refine
from hmm.platform.clock import SystemClock


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hmm-parser", description="Dry run: print EventImport JSONL to stdout."
    )
    parser.add_argument("--config", type=Path, default=None, help="sources TOML")
    parser.add_argument(
        "--source", action="append", default=[], help="source id to run (repeatable)"
    )
    parser.add_argument("--list", action="store_true", help="list configured sources and exit")
    parser.add_argument(
        "--llm",
        action="store_true",
        help="refine llm-enabled sources with LLM_BASE_URL / LLM_API_KEY / LLM_MODEL",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def llm_config(env: Mapping[str, str]) -> LLMConfig | None:
    base_url, api_key, model = (
        env.get(name) for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
    )
    if not (base_url and api_key and model):
        return None
    return LLMConfig(base_url=base_url, api_key=SecretStr(api_key), model=model)


def select_sources(sources: list[SourceConfig], wanted: list[str]) -> list[SourceConfig]:
    if not wanted:
        return [source for source in sources if source.enabled]
    known = {source.id: source for source in sources}
    unknown = [name for name in wanted if name not in known]
    if unknown:
        raise SourceConfigError(f"Unknown source ids: {', '.join(unknown)}")
    return [known[name] for name in wanted]


async def dry_run(args: argparse.Namespace, log: logging.Logger) -> int:
    path = args.config or (
        Path(os.environ["PARSER_SOURCES"]) if os.environ.get("PARSER_SOURCES") else None
    )
    sources = load_sources(path)
    if args.list:
        for source in sources:
            state = "enabled" if source.enabled else "disabled"
            print(f"{source.id}\t{source.adapter}\t{state}\t{source.interval}")
        return 0
    chosen = []
    for source in select_sources(sources, args.source):
        missing = source.missing_secrets(os.environ)
        if missing:
            log.warning("skipping %s: missing %s", source.id, ", ".join(missing))
            continue
        chosen.append(source)
    llm = llm_config(os.environ) if args.llm else None
    if args.llm and llm is None:
        raise SourceConfigError("--llm needs LLM_BASE_URL, LLM_API_KEY and LLM_MODEL")
    now = SystemClock().now()
    refiner: Refiner | None = None
    async with httpx.AsyncClient() as client:
        fetcher = Fetcher(client=client)
        runs = await asyncio.gather(
            *(
                collect(
                    source,
                    fetcher=fetcher,
                    now=now,
                    areas=frozenset(SEED_AREA_KEYS),
                    env=os.environ,
                    log=log,
                )
                for source in chosen
            )
        )
        if llm is not None:
            refiner = Refiner(
                client=client,
                config=llm,
                cache=MemoryCache(),
                areas=frozenset(SEED_AREA_KEYS),
                session=f"hmm-parser-dry-{now:%Y%m%dT%H%M%S}",
                log=log,
            )
            await refine(runs, refiner)
    items, merged = dedupe(runs)
    for item in items:
        sys.stdout.write(item.model_dump_json(exclude_defaults=True) + "\n")
    for run in runs:
        log.info("%s: %s", run.source.id, json.dumps(run.stats()))
        for error in run.errors:
            log.warning("%s: %s", run.source.id, json.dumps(error, ensure_ascii=False))
    log.info("%d items, %d merged, %d requests", len(items), merged, fetcher.requests)
    if refiner is not None:
        log.info(
            "llm: %d calls, %d failed, %d over the limit",
            refiner.calls,
            refiner.failed,
            refiner.skipped,
        )
    return 1 if any(run.errors for run in runs) else 0


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.INFO if args.verbose else logging.WARNING)
    log = logging.getLogger("hmm.parser")
    try:
        code = asyncio.run(dry_run(args, log))
    except SourceConfigError as error:
        log.error("%s", error)
        code = 2
    raise SystemExit(code)
