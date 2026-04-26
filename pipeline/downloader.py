import logging
from pathlib import Path

import aiofiles
from telegram import Bot

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}


def get_job_dir(job_id: str) -> Path:
    from config import TMP_DIR
    d = TMP_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


async def download_video(bot: Bot, file_id: str, job_id: str) -> Path:
    """Download a Telegram file to /tmp/clipcut/{job_id}/source.mp4.
    Returns path to the saved file."""
    job_dir = get_job_dir(job_id)
    dest = job_dir / "source.mp4"

    tg_file = await bot.get_file(file_id)
    logger.info("Downloading file_id=%s to %s", file_id, dest)
    await tg_file.download_to_drive(str(dest))
    logger.info("Download complete: %s (%.1f MB)", dest, dest.stat().st_size / 1024 / 1024)
    return dest
