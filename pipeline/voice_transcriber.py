"""
pipeline/voice_transcriber.py — транскрипция коротких голосовых сообщений.

Отличается от основного transcriber.py тем, что:
  1. Работает с короткими OGG Opus файлами (голос из Telegram, секунды, не минуты)
  2. Конвертирует OGG → WAV через ffmpeg напрямую в памяти (/tmp)
  3. Не делает поллинг статуса (AssemblyAI умеет sync-режим для коротких файлов)
  4. Возвращает только итоговый текст (word timestamps не нужны — это не видео)

AssemblyAI поддерживает прямую загрузку WAV через upload endpoint.
Для голосовых до 30 сек используем synchronous transcript с polling 1 сек.
"""

import asyncio
import httpx
import logging
import os
import subprocess
import tempfile

from config import ASSEMBLYAI_API_KEY

logger = logging.getLogger(__name__)

ASSEMBLYAI_UPLOAD_URL = "https://api.assemblyai.com/v2/upload"
ASSEMBLYAI_TRANSCRIPT_URL = "https://api.assemblyai.com/v2/transcript"

HEADERS = {
    "authorization": ASSEMBLYAI_API_KEY,
    "content-type": "application/json",
}


# ──────────────────────────────────────────────────────────────────────────────
# Конвертация OGG Opus → WAV
# ──────────────────────────────────────────────────────────────────────────────

def _ogg_to_wav(ogg_path: str) -> str:
    """
    Конвертирует OGG Opus (Telegram voice) в WAV 16kHz mono.
    Возвращает путь к временному WAV-файлу.
    Вызывает subprocess.CalledProcessError если ffmpeg упал.
    """
    wav_path = ogg_path.replace(".ogg", "_voice.wav")

    cmd = [
        "ffmpeg", "-y",
        "-i", ogg_path,
        "-ar", "16000",      # 16 kHz — оптимально для speech recognition
        "-ac", "1",          # mono
        "-f", "wav",
        wav_path,
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0:
        logger.error(f"ffmpeg OGG→WAV failed:\n{result.stderr}")
        raise RuntimeError(f"ffmpeg conversion failed: {result.stderr[-300:]}")

    logger.info(f"Converted OGG→WAV: {ogg_path} → {wav_path}")
    return wav_path


# ──────────────────────────────────────────────────────────────────────────────
# Загрузка WAV в AssemblyAI
# ──────────────────────────────────────────────────────────────────────────────

async def _upload_wav(wav_path: str, client: httpx.AsyncClient) -> str:
    """
    Загружает WAV в AssemblyAI upload endpoint.
    Возвращает upload_url для последующей транскрипции.
    """
    with open(wav_path, "rb") as f:
        audio_data = f.read()

    response = await client.post(
        ASSEMBLYAI_UPLOAD_URL,
        content=audio_data,
        headers={
            "authorization": ASSEMBLYAI_API_KEY,
            "content-type": "application/octet-stream",
        },
        timeout=60.0,
    )
    response.raise_for_status()

    upload_url = response.json()["upload_url"]
    logger.info(f"WAV uploaded to AssemblyAI: {upload_url[:60]}...")
    return upload_url


# ──────────────────────────────────────────────────────────────────────────────
# Транскрипция через AssemblyAI
# ──────────────────────────────────────────────────────────────────────────────

async def _transcribe_url(upload_url: str, client: httpx.AsyncClient) -> str:
    """
    Запускает транскрипцию на AssemblyAI и дожидается результата.
    Поллинг каждые 2 секунды. Таймаут 120 секунд.
    language_detection=True — автоматически определит RU/EN/другой язык.
    """
    # Создаём задачу транскрипции
    payload = {
        "audio_url": upload_url,
        "language_detection": True,
        "speech_models": ["universal-2"],
    }

    create_response = await client.post(
        ASSEMBLYAI_TRANSCRIPT_URL,
        json=payload,
        headers=HEADERS,
        timeout=30.0,
    )
    if create_response.status_code != 200:
        logger.error(f"AssemblyAI error: {create_response.text}")
    create_response.raise_for_status()

    transcript_id = create_response.json()["id"]
    poll_url = f"{ASSEMBLYAI_TRANSCRIPT_URL}/{transcript_id}"
    logger.info(f"AssemblyAI transcript created: {transcript_id}")

    # Поллинг статуса
    elapsed = 0
    poll_interval = 2  # секунды
    max_wait = 120     # секунды

    while elapsed < max_wait:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        status_response = await client.get(
            poll_url,
            headers={"authorization": ASSEMBLYAI_API_KEY},
            timeout=15.0,
        )
        status_response.raise_for_status()
        data = status_response.json()

        status = data.get("status")
        logger.debug(f"Transcript {transcript_id} status: {status} ({elapsed}s)")

        if status == "completed":
            text = data.get("text", "").strip()
            logger.info(f"Voice transcribed ({len(text)} chars): {text[:80]}")
            return text

        if status == "error":
            error = data.get("error", "unknown error")
            raise RuntimeError(f"AssemblyAI transcription error: {error}")

        # статус "queued" или "processing" — продолжаем ждать

    raise TimeoutError(f"AssemblyAI transcription timeout after {max_wait}s")


# ──────────────────────────────────────────────────────────────────────────────
# Публичная функция
# ──────────────────────────────────────────────────────────────────────────────

async def transcribe_voice_ogg(ogg_path: str) -> str:
    """
    Полный pipeline транскрипции голосового сообщения:
      OGG (Telegram) → WAV 16kHz → AssemblyAI upload → transcript → текст

    Args:
        ogg_path: путь к .ogg файлу, скачанному из Telegram

    Returns:
        Распознанный текст. Пустая строка если речи нет.

    Raises:
        RuntimeError: если ffmpeg упал или AssemblyAI вернул ошибку
        TimeoutError: если транскрипция заняла > 120 сек
    """
    wav_path = None
    try:
        # Шаг 1: конвертация в WAV (sync, быстро)
        wav_path = _ogg_to_wav(ogg_path)

        # Шаг 2: загрузка и транскрипция (async)
        async with httpx.AsyncClient() as client:
            upload_url = await _upload_wav(wav_path, client)
            text = await _transcribe_url(upload_url, client)

        return text

    finally:
        # Удаляем временный WAV — OGG оставляем как кеш
        if wav_path and os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except OSError:
                pass
