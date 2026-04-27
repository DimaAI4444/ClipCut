import asyncio
import json
import logging
from pathlib import Path

from telegram import Update, Message
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from config import MAX_FILE_SIZE_BYTES, MAX_REVISIONS, TMP_DIR
from db.jobs import (
    create_job,
    get_latest_job_for_user,
    get_job,
    update_status,
    update_progress_msg_id,
    upsert_user,
    increment_user_jobs,
    is_whitelisted,
    save_feedback,
)
from pipeline.downloader import ALLOWED_EXTENSIONS
from bot.keyboards import kb_after_result, kb_cancel
from bot.voice_handler import handle_voice_idle, handle_voice_params, handle_voice_revision
from bot.chat_handler import handle_chat_text, handle_chat_voice

logger = logging.getLogger(__name__)

# FSM states
WAITING_VIDEO, WAITING_PARAMS, WAITING_REVISION, WAITING_FEEDBACK = range(4)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _check_whitelist(update: Update) -> bool:
    user_id = update.effective_user.id
    if not await is_whitelisted(user_id):
        await update.message.reply_text(
            "🔒 Вы в листе ожидания.\n\nBeta-доступ ограничен. "
            "Напишите @ddrozdov_ai чтобы получить доступ."
        )
        return False
    return True


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    await upsert_user(user.id, user.username)

    if not await _check_whitelist(update):
        return ConversationHandler.END

    await update.message.reply_text(
        "Привет! Я ClipCut AI ✂️\n\n"
        "Отправь мне видео-исходник — я уберу дубли, запинки и мусор, "
        "и верну тебе чистое готовое видео.\n\n"
        "Как пользоваться:\n"
        "1. Отправь видео-файл (до 2 ГБ, форматы: MP4, MOV, AVI, MKV)\n"
        "2. Скажи, какой хронометраж нужен на выходе\n"
        "3. Жди 2–5 минут\n"
        "4. Получай результат + можешь попросить правки\n\n"
        "Отправляй видео 👇"
    )
    return WAITING_VIDEO


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ClipCut AI ✂️ — краткий справочник\n\n"
        "Команды:\n"
        "/start  — начать сначала\n"
        "/status — статус текущего задания\n"
        "/cancel — отменить задание\n"
        "/help   — эта справка\n"
        "/feedback — написать разработчику\n\n"
        "Лимиты:\n"
        "• Файл до 2 ГБ\n"
        "• До 3 правок на одно видео\n"
        "• Форматы: MP4, MOV, AVI, MKV\n\n"
        "Вопросы? @ddrozdov_ai"
    )


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    job = await get_latest_job_for_user(update.effective_user.id)
    if not job:
        await update.message.reply_text("У вас нет активных заданий.")
        return
    status_labels = {
        "queued": "⏳ В очереди",
        "processing": "🔄 Обрабатывается",
        "done": "✅ Готово",
        "error": "❌ Ошибка",
        "archived": "🗄 Архив",
    }
    label = status_labels.get(job["status"], job["status"])
    await update.message.reply_text(f"Статус последнего задания: {label}")


# ---------------------------------------------------------------------------
# /cancel
# ---------------------------------------------------------------------------

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    job = await get_latest_job_for_user(update.effective_user.id)
    if job and job["status"] in ("queued", "processing"):
        await update_status(job["id"], "error", error_msg="Отменено пользователем")
    await update.message.reply_text("Задание отменено. Отправь новое видео когда будешь готов.")
    context.user_data.clear()
    return WAITING_VIDEO


# ---------------------------------------------------------------------------
# /feedback
# ---------------------------------------------------------------------------

async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Напиши свой отзыв или опиши проблему — отправлю разработчику:"
    )
    return WAITING_FEEDBACK


async def receive_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    job = await get_latest_job_for_user(update.effective_user.id)
    job_id = job["id"] if job else None
    await save_feedback(update.effective_user.id, job_id, update.message.text)
    await update.message.reply_text("Спасибо! Отзыв получен ✅")
    return WAITING_VIDEO


# ---------------------------------------------------------------------------
# Receive video
# ---------------------------------------------------------------------------

async def receive_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _check_whitelist(update):
        return ConversationHandler.END

    # Accept video or document
    doc = update.message.document
    video = update.message.video

    if doc:
        file_id = doc.file_id
        file_size = doc.file_size or 0
        file_name = doc.file_name or "source.mp4"
        ext = Path(file_name).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            await update.message.reply_text(
                "Это не видео 🤔\n\n"
                "Пришли видео-файл в формате MP4, MOV, AVI или MKV.\n"
                "Ссылки на YouTube или облако не подходят — только файл."
            )
            return WAITING_VIDEO
    elif video:
        file_id = video.file_id
        file_size = video.file_size or 0
        file_name = "source.mp4"
    else:
        await update.message.reply_text(
            "Это не видео 🤔\n\n"
            "Пришли видео-файл в формате MP4, MOV, AVI или MKV.\n"
            "Ссылки на YouTube или облако не подходят — только файл."
        )
        return WAITING_VIDEO

    if file_size > MAX_FILE_SIZE_BYTES:
        size_gb = file_size / 1024 / 1024 / 1024
        await update.message.reply_text(
            f"❌ Файл слишком большой ({size_gb:.2f} ГБ).\n\n"
            "Telegram ограничивает загрузку файлов до 2 ГБ.\n\n"
            "Как исправить:\n"
            "• Сожми видео через HandBrake (бесплатно) — качество почти не потеряется\n"
            "• Или обрежь исходник на части и пришли по отдельности"
        )
        return WAITING_VIDEO

    context.user_data["file_id"] = file_id
    context.user_data["file_name"] = file_name
    context.user_data["file_size"] = file_size

    size_mb = file_size / 1024 / 1024
    await update.message.reply_text(
        f"Видео получено ✅ ({file_name}, {size_mb:.1f} МБ)\n\n"
        "Скажи, что нужно сделать:\n"
        "• Какой хронометраж хочешь на выходе? (например: «60-90 секунд» или «до 2 минут»)\n"
        "• Есть особые пожелания? (например: «убери все места где я запинаюсь»)\n\n"
        "Если ничего не писать — сделаю оптимальную нарезку сам.\n\n"
        "Напиши параметры или отправь /ok чтобы начать без них 👇"
    )
    return WAITING_PARAMS


# ---------------------------------------------------------------------------
# Receive params → enqueue job
# ---------------------------------------------------------------------------

async def _enqueue_job(update: Update, context: ContextTypes.DEFAULT_TYPE, target_duration: str, notes: str) -> int:
    user_id = update.effective_user.id
    file_id = context.user_data.get("file_id")
    if not file_id:
        await update.message.reply_text("Видео не найдено. Пожалуйста, отправь его снова.")
        return WAITING_VIDEO

    # Create placeholder path — downloader will fill it
    job_id = await create_job(
        user_id=user_id,
        file_path="",
        target_duration=target_duration,
        notes=notes,
    )
    context.user_data["job_id"] = job_id
    context.user_data["file_id_for_download"] = file_id

    msg: Message = await update.message.reply_text(
        "⏳ Начинаю обработку...\n\nШаг 1/3 — Транскрибирую речь",
        reply_markup=kb_cancel(),
    )
    await update_progress_msg_id(job_id, msg.message_id)

    await increment_user_jobs(user_id)

    # Trigger worker via context (store file_id so worker can download)
    context.application.bot_data.setdefault("pending_downloads", {})[job_id] = file_id

    logger.info("Job %s enqueued for user %d", job_id, user_id)

    # Start polling for job completion
    asyncio.create_task(_poll_job_status(context.application, user_id, job_id, msg.message_id))

    return WAITING_REVISION


async def receive_params(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    return await _enqueue_job(update, context, target_duration=text, notes="")


async def cmd_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _enqueue_job(update, context, target_duration="оптимальный", notes="")


# ---------------------------------------------------------------------------
# Poll job status (background task per user)
# ---------------------------------------------------------------------------

async def _poll_job_status(app, user_id: int, job_id: str, progress_msg_id: int) -> None:
    from config import STATUS_POLL_INTERVAL
    from pipeline.cutter import format_duration

    stage_texts = {
        "transcribing": "⏳ Шаг 1/3 — Транскрибирую речь...",
        "analyzing":    "⏳ Шаг 2/3 — Анализирую дубли и мусор...",
        "cutting":      "⏳ Шаг 3/3 — Нарезаю видео...",
    }

    edit_errors = 0
    while True:
        await asyncio.sleep(STATUS_POLL_INTERVAL)
        job = await get_job(job_id)
        if not job:
            break

        progress = app.bot_data.get("progress", {}).get(job_id)
        if progress and progress in stage_texts:
            try:
                await app.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=progress_msg_id,
                    text=stage_texts[progress],
                    reply_markup=kb_cancel(),
                )
                edit_errors = 0
            except Exception:
                edit_errors += 1
                if edit_errors > 5:
                    logger.warning("Too many edit errors for job %s, stopping progress updates", job_id)
                    progress_msg_id = None

        if job["status"] == "done":
            result_path = Path(job["result_path"])
            orig_dur = job.get("original_dur_s") or 0
            res_dur = job.get("result_dur_s") or 0

            # Load summary from cut_plan
            rev = job.get("revision", 0)
            plan_path = TMP_DIR / job_id / f"cut_plan_v{rev}.json"
            summary = ""
            removed_count = 0
            if plan_path.exists():
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                summary = plan.get("summary", "")
                removed_count = len(plan.get("remove", []))

            revisions_left = MAX_REVISIONS - rev
            caption = (
                "✅ Готово!\n\n"
                f"📊 Что сделано:\n"
                f"• Исходник: {format_duration(orig_dur)}\n"
                f"• Результат: {format_duration(res_dur)}\n"
                f"• Убрано фрагментов: {removed_count}\n\n"
                f"{summary}\n\n"
                f"Хочешь что-то исправить? Напиши — у тебя ещё {revisions_left} правки.\n"
                "Или /cancel если всё ок."
            )
            try:
                await app.bot.delete_message(chat_id=user_id, message_id=progress_msg_id)
            except Exception:
                pass
            try:
                await app.bot.send_document(
                    chat_id=user_id,
                    document=result_path.open("rb"),
                    filename=result_path.name,
                    caption=caption,
                    reply_markup=kb_after_result(revisions_left),
                )
            except Exception as e:
                logger.error("Failed to send result for job %s: %s", job_id, e)
                await app.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ Не удалось отправить файл: {e}",
                )
            break

        if job["status"] == "error":
            err = job.get("error_msg", "Неизвестная ошибка")
            try:
                await app.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=progress_msg_id,
                    text=(
                        "😔 Что-то пошло не так при обработке.\n\n"
                        f"Причина: {err}\n\n"
                        "Попробуй:\n"
                        "1. Пересохранить видео через любой конвертер в MP4 (H.264)\n"
                        "2. Отправить снова\n\n"
                        "Если не помогло — напиши /feedback, разберёмся."
                    ),
                )
            except Exception:
                pass
            break


# ---------------------------------------------------------------------------
# Revision
# ---------------------------------------------------------------------------

async def receive_revision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    job_id = context.user_data.get("job_id")
    if not job_id:
        job = await get_latest_job_for_user(update.effective_user.id)
        if job:
            job_id = job["id"]
            context.user_data["job_id"] = job_id

    if not job_id:
        await update.message.reply_text("Нет активного задания. Отправь видео.")
        return WAITING_VIDEO

    job = await get_job(job_id)
    if not job or job["status"] != "done":
        await update.message.reply_text("Подожди — задание ещё обрабатывается.")
        return WAITING_REVISION

    current_rev = job.get("revision", 0)
    if current_rev >= MAX_REVISIONS:
        await update.message.reply_text(
            f"Лимит правок на это видео исчерпан ({MAX_REVISIONS}/{MAX_REVISIONS}).\n\n"
            "Чтобы продолжить — отправь исходник заново, "
            "и в параметрах опиши точнее что нужно."
        )
        context.user_data.clear()
        return WAITING_VIDEO

    user_text = update.message.text.strip()
    msg = await update.message.reply_text(
        f"Понял, делаю правку ✏️\n\n"
        f"Правка {current_rev + 1}/{MAX_REVISIONS}: «{user_text[:100]}»\n\n"
        "⏳ Обрабатываю..."
    )

    # Enqueue revision as new processing
    await update_status(job_id, "queued")
    context.application.bot_data.setdefault("revisions", {})[job_id] = {
        "user_text": user_text,
        "progress_msg_id": msg.message_id,
        "user_id": update.effective_user.id,
    }

    asyncio.create_task(
        _poll_job_status(
            context.application,
            update.effective_user.id,
            job_id,
            msg.message_id,
        )
    )

    return WAITING_REVISION


# ---------------------------------------------------------------------------
# Callback queries (inline buttons)
# ---------------------------------------------------------------------------

async def cb_want_revision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "Напиши что нужно исправить:\n\n"
        "Примеры:\n"
        "• «слишком коротко, добавь ещё 30 секунд»\n"
        "• «убери кусок где я говорю про цену»\n"
        "• «верни секунды 15–25»"
    )
    return WAITING_REVISION


async def cb_done_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer("Отлично! 🎉")
    await update.callback_query.message.reply_text(
        "Готово! Отправляй новое видео когда понадоблюсь ✂️"
    )
    context.user_data.clear()
    return WAITING_VIDEO


async def cb_cancel_job(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer("Отменяю...")
    job_id = context.user_data.get("job_id")
    if job_id:
        await update_status(job_id, "error", error_msg="Отменено пользователем")
    await update.callback_query.message.edit_text("❌ Задание отменено.")
    context.user_data.clear()
    return WAITING_VIDEO


# ---------------------------------------------------------------------------
# Voice helpers
# ---------------------------------------------------------------------------

async def _voice_in_idle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await handle_voice_idle(update, context)
    return WAITING_VIDEO


async def _voice_in_waiting_params(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await handle_voice_params(update, context)
    return WAITING_REVISION


async def _voice_in_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await handle_voice_revision(update, context)
    return WAITING_REVISION


# ---------------------------------------------------------------------------
# Chat wrappers — LLM replies while staying in current FSM state
# ---------------------------------------------------------------------------

async def standalone_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Срабатывает только если пользователь вне ConversationHandler."""
    if context.user_data.get("job_id") or context.user_data.get("file_id"):
        return
    await handle_chat_text(update, context, current_state=None)

async def standalone_chat_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Голос вне ConversationHandler — только если FSM не активен."""
    if context.user_data.get("job_id") or context.user_data.get("file_id"):
        return
    await handle_chat_voice(update, context, current_state=None)


# ---------------------------------------------------------------------------
# Build Application
# ---------------------------------------------------------------------------

def build_app(token: str) -> Application:
    app = (
        Application.builder()
        .token(token)
        .base_url("http://localhost:8081/bot")
        .base_file_url("http://localhost:8081/file/bot")
        .get_updates_read_timeout(30)
        .read_timeout(300)
        .write_timeout(300)
        .connect_timeout(30)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            MessageHandler(filters.VIDEO | filters.Document.VIDEO, receive_video),
            MessageHandler(filters.VOICE, _voice_in_idle),
        ],
        states={
            WAITING_VIDEO: [
                MessageHandler(filters.VIDEO | filters.Document.ALL, receive_video),
                MessageHandler(filters.VOICE, _voice_in_idle),
            ],
            WAITING_PARAMS: [
                CommandHandler("ok", cmd_ok),
                MessageHandler(filters.VOICE, _voice_in_waiting_params),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_params),
            ],
            WAITING_REVISION: [
                MessageHandler(filters.VOICE, _voice_in_done),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_revision),
                CallbackQueryHandler(cb_want_revision, pattern="^want_revision$"),
                CallbackQueryHandler(cb_done_ok, pattern="^done_ok$"),
            ],
            WAITING_FEEDBACK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_feedback),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("start", cmd_start),
            MessageHandler(filters.VOICE, _voice_in_idle),
        ],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, standalone_chat), group=1)
    app.add_handler(MessageHandler(filters.VOICE, standalone_chat_voice), group=1)
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("feedback", cmd_feedback))
    app.add_handler(CallbackQueryHandler(cb_cancel_job, pattern="^cancel_job$"))

    return app
