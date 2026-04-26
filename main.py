import asyncio
import logging
import sys

# Windows requires SelectorEventLoop for subprocess support
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from config import TELEGRAM_TOKEN
from db.jobs import init_db
from bot.handlers import build_app
from worker import run_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


async def main() -> None:
    logger.info("Initialising database...")
    await init_db()

    logger.info("Building Telegram application...")
    app = build_app(TELEGRAM_TOKEN)

    logger.info("Starting bot + worker...")
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        # Run worker in background
        worker_task = asyncio.create_task(run_worker(app))

        try:
            # Keep running until interrupted
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutdown signal received")
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
            await app.updater.stop()
            await app.stop()

    logger.info("Stopped.")


if __name__ == "__main__":
    asyncio.run(main())
