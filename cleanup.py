"""
cleanup.py — удаляет файлы заданий старше CLEANUP_AFTER_HOURS часов.
Запускать через cron каждый час:
    0 * * * * /opt/clipcut/venv/bin/python /opt/clipcut/cleanup.py
На Windows — через Task Scheduler.
"""
import asyncio
import glob
import logging
import shutil
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def cleanup_old_jobs() -> None:
    from config import TMP_DIR, CLEANUP_AFTER_HOURS
    from db.jobs import init_db, mark_archived

    await init_db()

    cutoff = time.time() - CLEANUP_AFTER_HOURS * 3600
    removed = 0

    for job_dir in Path(TMP_DIR).iterdir():
        if not job_dir.is_dir():
            continue
        mtime = job_dir.stat().st_mtime
        if mtime < cutoff:
            job_id = job_dir.name
            try:
                shutil.rmtree(job_dir)
                await mark_archived(job_id)
                logger.info("Removed job dir: %s", job_dir)
                removed += 1
            except Exception as e:
                logger.error("Failed to remove %s: %s", job_dir, e)

    logger.info("Cleanup done: %d dirs removed", removed)


if __name__ == "__main__":
    asyncio.run(cleanup_old_jobs())
