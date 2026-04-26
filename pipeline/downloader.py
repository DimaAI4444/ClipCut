import logging
import shutil
from pathlib import Path
from telegram import Bot

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}


def get_job_dir(job_id: str) -> Path:
    from config import TMP_DIR
    d = TMP_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


async def download_video(bot: Bot, file_id: str, job_id: str) -> Path:
    job_dir = get_job_dir(job_id)
    dest = job_dir / "source.mp4"

    tg_file = await bot.get_file(file_id)
    file_path = tg_file.file_path

    logger.info("file_path from API: %s", file_path)

    if file_path and file_path.startswith("/"):
        local_path = Path(file_path)
        if local_path.exists():
            logger.info("Copying local file %s to %s", local_path, dest)
            shutil.copy2(str(local_path), str(dest))
        else:
            mapped = file_path.replace("/var/lib/telegram-bot-api", "/opt/telegram-bot-api")
            host_path = Path(mapped)
            logger.info("Mapped host path: %s", host_path)
            shutil.copy2(str(host_path), str(dest))
    else:
        logger.info("Downloading via URL: %s", file_path)
        await tg_file.download_to_drive(str(dest))

    logger.info("Download complete: %s (%.1f MB)", dest, dest.stat().st_size / 1024 / 1024)
    return dest
