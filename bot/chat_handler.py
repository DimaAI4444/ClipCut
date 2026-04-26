"""
bot/chat_handler.py — «Умный» чат вне и внутри FSM.

Логика:
- Перехватывает текст и голос в любом состоянии FSM через отдельные хендлеры.
- Вызывает LLM с системным промптом ClipCut-ассистента.
- НЕ меняет состояние FSM — возвращает текущее состояние обратно.
- Используется двумя способами:
    1. Как standalone handler (group=1) — для IDLE / вне ConversationHandler.
    2. Импортируется в handlers.py и вызывается внутри состояний FSM.
"""

import logging
from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import ContextTypes
from pipeline.voice_transcriber import transcribe_voice_ogg
from config import OPENROUTER_API_KEY, TMP_DIR
import os

logger = logging.getLogger(__name__)

# Системный промпт ассистента ClipCut
CHAT_SYSTEM_PROMPT = """Ты — ClipCut AI, Telegram-бот для автоматической нарезки видео. \
Ты сам принимаешь видео, сам нарезаешь и отдаёшь результат. \
Не говори пользователю «отправь боту» или «бот сделает» — ты и есть этот бот.

ClipCut AI — бот с одной задачей: берёт видео-исходник и возвращает чистую нарезку.
Что он делает:
- Транскрибирует речь с точными таймкодами (AssemblyAI)
- Анализирует: убирает дубли, запинки, паузы, технический мусор
- Нарезает видео через FFmpeg
- Принимает правки — до 3 итераций, текстом или голосом

Как работать с ботом:
1. Отправить видео-файл (до 2 ГБ, MP4/MOV/AVI/MKV)
2. Сказать желаемый хронометраж — текстом или голосом
3. Ждать 2–5 минут
4. Получить результат, при желании — попросить правки

Команды: /start, /status, /cancel, /help, /feedback

ТВОЯ РОЛЬ:
- Отвечай на вопросы про ClipCut, монтаж, нарезку видео — развёрнуто и по делу.
- Если пользователь описывает задачу с видео — помоги понять, как сформулировать параметры для бота.
- Если вопрос совсем не по теме (погода, рецепты, политика и т.д.) — ответь коротко и мягко верни к теме:
  «Я специализируюсь на нарезке видео — если есть исходник, отправляй!»
- Будь дружелюбным, лаконичным. Не используй длинные списки без необходимости.
- Отвечай на языке пользователя (русский или английский).

ФОРМАТ ОТВЕТА:
Используй Telegram HTML. Только эти теги:
- <b>жирный</b>
- <i>курсив</i>
- <code>код</code>
- <a href="url">ссылка</a>
Никаких **, *, #, MarkdownV2. Только HTML-теги выше.
"""


async def _call_chat_llm(user_message: str, history: list[dict]) -> str:
    """
    Вызывает LLM через OpenRouter для чат-ответа.
    history — список предыдущих сообщений [{role, content}] для контекста.
    """
    client = AsyncOpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
    )

    messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
    messages.extend(history[-6:])  # последние 3 пары вопрос/ответ для контекста
    messages.append({"role": "user", "content": user_message})

    response = await client.chat.completions.create(
        model="anthropic/claude-sonnet-4-5",
        messages=messages,
        max_tokens=512,
        temperature=0.7,
    )
    return response.choices[0].message.content.strip()


def _get_chat_history(context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    """Возвращает историю чата из user_data, инициализирует если нет."""
    if "chat_history" not in context.user_data:
        context.user_data["chat_history"] = []
    return context.user_data["chat_history"]


def _append_to_history(context: ContextTypes.DEFAULT_TYPE, role: str, content: str) -> None:
    """Добавляет сообщение в историю, обрезает до последних 20 записей."""
    history = _get_chat_history(context)
    history.append({"role": role, "content": content})
    context.user_data["chat_history"] = history[-20:]


async def handle_chat_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    current_state: int | None = None,
) -> int | None:
    """
    Обработчик произвольного текста.

    Вызывается:
    - Из состояний FSM (передаётся current_state — возвращается обратно).
    - Как standalone handler вне ConversationHandler (current_state=None).

    Возвращает current_state чтобы FSM не менял состояние.
    """
    user_text = update.message.text
    if not user_text:
        return current_state

    history = _get_chat_history(context)

    try:
        await update.message.chat.send_action("typing")
        reply = await _call_chat_llm(user_text, history)
    except Exception as e:
        logger.error(f"Chat LLM error: {e}")
        reply = (
            "Что-то пошло не так с ответом. Попробуй ещё раз или отправь видео — "
            "с этим я точно справлюсь ✂️"
        )

    _append_to_history(context, "user", user_text)
    _append_to_history(context, "assistant", reply)

    try:
        await update.message.reply_text(reply, parse_mode="HTML")
    except Exception:
        await update.message.reply_text(reply)
    return current_state


async def handle_chat_voice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    current_state: int | None = None,
) -> int | None:
    """
    Обработчик голосового сообщения в чат-режиме.

    Транскрибирует через AssemblyAI, затем передаёт в handle_chat_text.
    Вызывается из состояний FSM или как standalone handler.
    """
    voice = update.message.voice
    if not voice:
        return current_state

    # Скачиваем OGG
    voice_dir = os.path.join(TMP_DIR, "voice")
    os.makedirs(voice_dir, exist_ok=True)
    ogg_path = os.path.join(voice_dir, f"{voice.file_id}.ogg")

    try:
        await update.message.chat.send_action("typing")

        # Кеш по file_id — не скачиваем повторно
        if not os.path.exists(ogg_path):
            tg_file = await context.bot.get_file(voice.file_id)
            await tg_file.download_to_drive(ogg_path)

        recognized_text = await transcribe_voice_ogg(ogg_path)

    except Exception as e:
        logger.error(f"Voice transcription error in chat mode: {e}")
        await update.message.reply_text(
            "😔 Не удалось распознать голосовое. Попробуй ещё раз или напиши текстом."
        )
        return current_state

    if not recognized_text or not recognized_text.strip():
        await update.message.reply_text(
            "😔 Не расслышал — тишина или сильный шум. Напиши текстом."
        )
        return current_state

    # Показываем что распознали, затем отвечаем
    await update.message.reply_text(f"🎙 Распознал: «{recognized_text}»")

    # Переиспользуем текстовый обработчик
    # Подменяем update.message.text для единообразия
    update.message.text = recognized_text
    return await handle_chat_text(update, context, current_state)
