import asyncio
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from hmm.bot.commands import bot_commands
from hmm.bot.delivery import Deliverer
from hmm.bot.handlers import build_router
from hmm.bot.runtime import BotRuntime
from hmm.bot.templates import Rendered
from hmm.common import Locale
from hmm.platform.clock import SystemClock
from hmm.platform.config import Config
from hmm.platform.db import create_engine, create_sessionmaker
from hmm.platform.jobs import create_jobs_app, register_jobs
from hmm.platform.logs import configure_logging


def build_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(build_router())
    return dispatcher


async def run(config: Config) -> None:
    if not config.database_url:
        raise RuntimeError("DATABASE_URL is required to run the bot")
    if config.bot_token is None:
        raise RuntimeError("BOT_TOKEN is required to run the bot")
    engine = create_engine(config.database_url)
    runtime = BotRuntime(
        config=config,
        sessionmaker=create_sessionmaker(engine),
        jobs=register_jobs(create_jobs_app(config.database_url)),
        clock=SystemClock(),
    )
    bot = Bot(
        config.bot_token.get_secret_value(),
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )

    async def send(chat_id: int, rendered: Rendered) -> int:
        message = await bot.send_message(
            chat_id,
            rendered.text,
            reply_markup=rendered.markup,
            disable_notification=rendered.silent,
        )
        return message.message_id

    delivery = asyncio.create_task(Deliverer(runtime, send).run())
    try:
        await bot.set_my_commands(bot_commands(Locale.EN))
        await bot.set_my_commands(bot_commands(Locale.RU), language_code=Locale.RU)
        await build_dispatcher().start_polling(bot, runtime=runtime, close_bot_session=False)
    finally:
        delivery.cancel()
        with suppress(asyncio.CancelledError):
            await delivery
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    config = Config.from_env()
    configure_logging(config.log_level)
    asyncio.run(run(config))
