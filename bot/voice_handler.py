"""
voice_handler.py — приём голосовых сообщений от пользователя.

Голосовое сообщение используется как альтернативный способ задать параметры
нарезки (вместо текста). Telegram присылает voice как OGG Opus — конвертируем
в WAV через ffmpeg, затем транскрибируем через AssemblyAI (тот же клиент,
что и для видео). Возвращаем распознанный текст обратно в FSM как обычные
параметры и продолжаем стандартный pipeline.

Поддерживаемые состояния FSM где принимается голос:
  - WAITING_PARAMS  — параметры нарезки голосом
  - DONE            — правка голосом
  - IDLE            — команда /start / любое приветствие голосом
"""

import asyncio
import logging
import os
import tempfile

import aiofiles
from telegram import Update, Voice
from telegram.ext import ContextTypes

from config import TMP_DIR, ASSEMBLYAI_API_KEY
from pipeline.voice_transcriber import transcribe_voice_ogg

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ──────────────────────────────────────────────────────────────────────────────

async def _download_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """
    Скачивает голосовое сообщение из Telegram и сохраняет в /tmp/clipcut/voice/.
    Возвращает путь к OGG-файлу.
    """
    voice: Voice = update.message.voice
    voice_dir = os.path.join(TMP_DIR, "voice")
    os.makedirs(voice_dir, exist_ok=True)

    ogg_path = os.path.join(voice_dir, f"{voice.file_id}.ogg")

    if not os.path.exists(ogg_path):
        tg_file = await context.bot.get_file(voice.file_id)
        await tg_file.download_to_drive(ogg_path)
        logger.info(f"Voice downloaded: {ogg_path} ({voice.duration}s)")

    return ogg_path


async def _send_typing_and_transcribe(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ogg_path: str,
) -> str | None:
    """
    Отправляет «печатает...», транскрибирует OGG → текст.
    Возвращает распознанный текст или None при ошибке.
    """
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action="typing",
    )

    try:
        text = await transcribe_voice_ogg(ogg_path)
        return text.strip() if text else None
    except Exception as e:
        logger.error(f"Voice transcription failed: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Хендлер: голос в состоянии WAITING_PARAMS
# ──────────────────────────────────────────────────────────────────────────────

async def handle_voice_params(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Принимает голосовое сообщение как параметры нарезки.
    Эквивалентно тому, что пользователь написал текст параметров.
    """
    ogg_path = await _download_voice(update, context)

    status_msg = await update.message.reply_text(
        "👂 Слушаю тебя, распознаю речь..."
    )

    text = await _send_typing_and_transcribe(update, context, ogg_path)

    if not text:
        await status_msg.edit_text(
            "😔 Не удалось распознать голосовое. Попробуй ещё раз или напиши текстом."
        )
        return

    # Показываем что распознали — пользователь видит и может скорректировать
    await status_msg.edit_text(
        f"✅ Распознал: «{text}»\n\n"
        "⏳ Начинаю обработку..."
    )

    # Сохраняем распознанный текст в контекст — дальше стандартный flow
    context.user_data["voice_params_text"] = text

    # Передаём управление стандартному обработчику параметров
    # Эмулируем текстовое сообщение через прямой вызов handle_params
    from bot.handlers import _enqueue_job
    await _enqueue_job(update, context, target_duration=text, notes="")


# ──────────────────────────────────────────────────────────────────────────────
# Хендлер: голос в состоянии DONE (правка)
# ──────────────────────────────────────────────────────────────────────────────

async def handle_voice_revision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Принимает голосовое сообщение как текст правки.
    Эквивалентно тексту правки в состоянии DONE.
    """
    ogg_path = await _download_voice(update, context)

    status_msg = await update.message.reply_text(
        "👂 Слушаю правку, распознаю..."
    )

    text = await _send_typing_and_transcribe(update, context, ogg_path)

    if not text:
        await status_msg.edit_text(
            "😔 Не удалось распознать голосовое. Попробуй ещё раз или напиши правку текстом."
        )
        return

    await status_msg.edit_text(
        f"✅ Распознал правку: «{text}»\n\n"
        "✏️ Применяю..."
    )

    context.user_data["voice_revision_text"] = text
    update.message.text = text  # эмулируем текстовое сообщение
    from bot.handlers import receive_revision
    await receive_revision(update, context)


# ──────────────────────────────────────────────────────────────────────────────
# Хендлер: голос в состоянии IDLE (начало сессии)
# ──────────────────────────────────────────────────────────────────────────────

async def handle_voice_idle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Пользователь прислал голос в состоянии IDLE (ещё не загрузил видео).
    Деликатно напоминаем что сначала нужно видео.
    """
    await update.message.reply_text(
        "👂 Слышу тебя!\n\n"
        "Сначала отправь видео-файл (MP4, MOV, AVI, MKV) — "
        "потом я спрошу параметры, и там можно будет ответить голосом 👍"
    )
