async def download_video(bot: Bot, file_id: str, job_id: str) -> Path:
    job_dir = get_job_dir(job_id)
    dest = job_dir / "source.mp4"

    tg_file = await bot.get_file(file_id)
    file_path = tg_file.file_path

    logger.info("file_path from API: %s", file_path)

    # Локальный Bot API возвращает URL вида:
    # http://localhost:8081/file/bot<token>//var/lib/telegram-bot-api/<token>/videos/file.mp4
    # Извлекаем путь внутри контейнера и маппим на хост
    if "/var/lib/telegram-bot-api/" in file_path:
        inner = "/var/lib/telegram-bot-api/" + file_path.split("/var/lib/telegram-bot-api/")[1]
        host_path = Path(inner.replace("/var/lib/telegram-bot-api", "/opt/telegram-bot-api"))
        logger.info("Host path: %s", host_path)
        shutil.copy2(str(host_path), str(dest))
    elif file_path.startswith("/"):
        shutil.copy2(file_path, str(dest))
    else:
        await tg_file.download_to_drive(str(dest))

    logger.info("Download complete: %s (%.1f MB)", dest, dest.stat().st_size / 1024 / 1024)
    return dest
