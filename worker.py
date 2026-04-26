import asyncio
import json
import logging
from pathlib import Path

from config import TMP_DIR, WORKER_POLL_INTERVAL
from db.jobs import (
    get_next_queued_job,
    update_status,
    set_original_duration,
    increment_revision,
    get_current_revision,
)
from pipeline.downloader import download_video
from pipeline.transcriber import extract_audio, transcribe, get_video_duration
from pipeline.analyzer import analyze, apply_revision
from pipeline.cutter import cut_video, get_result_duration

logger = logging.getLogger(__name__)


async def run_worker(app) -> None:
    """Main worker loop. Polls DB every WORKER_POLL_INTERVAL seconds."""
    logger.info("Worker started")
    while True:
        try:
            job = await get_next_queued_job()
            if job:
                # Check if this is a revision job
                revisions = app.bot_data.get("revisions", {})
                if job["id"] in revisions:
                    await process_revision(app, job, revisions.pop(job["id"]))
                else:
                    await process_job(app, job)
        except Exception as e:
            logger.exception("Worker loop error: %s", e)
        await asyncio.sleep(WORKER_POLL_INTERVAL)


async def _update_progress(app, job_id: str, stage: str) -> None:
    """Update progress stage in bot_data (handlers poll this)."""
    app.bot_data.setdefault("progress", {})[job_id] = stage


async def process_job(app, job: dict) -> None:
    job_id = job["id"]
    user_id = job["user_id"]

    try:
        await update_status(job_id, "processing")
        logger.info("Processing job %s for user %d", job_id, user_id)

        job_dir = TMP_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        source = job_dir / "source.mp4"

        # Download if not already present
        pending = app.bot_data.get("pending_downloads", {})
        file_id = pending.pop(job_id, None)

        if not source.exists():
            if not file_id:
                raise RuntimeError("Source file missing and no file_id for download")
            await _update_progress(app, job_id, "transcribing")
            source = await download_video(app.bot, file_id, job_id)
        else:
            logger.info("Source already exists: %s", source)

        # Record original duration
        orig_dur = get_video_duration(source)
        await set_original_duration(job_id, orig_dur)

        # Extract audio
        await _update_progress(app, job_id, "transcribing")
        audio = extract_audio(source, job_id)

        # Transcribe
        transcript = await transcribe(audio)
        transcript_path = job_dir / "transcript.json"
        transcript_path.write_text(
            json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Analyze
        await _update_progress(app, job_id, "analyzing")
        params = {
            "target_duration": job.get("target_duration") or "оптимальный",
            "notes": job.get("user_notes") or "",
            "original_duration": f"{orig_dur:.1f}",
        }
        cut_plan = await analyze(transcript, params)
        plan_path = job_dir / "cut_plan_v0.json"
        plan_path.write_text(
            json.dumps(cut_plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Cut
        await _update_progress(app, job_id, "cutting")
        result = job_dir / "result_v0.mp4"
        cut_video(source, cut_plan, result)

        res_dur = get_result_duration(result)
        await update_status(job_id, "done", result_path=str(result), result_dur_s=res_dur)
        logger.info("Job %s done in %.1fs→%.1fs", job_id, orig_dur, res_dur)

    except Exception as e:
        logger.exception("Job %s failed: %s", job_id, e)
        await update_status(job_id, "error", error_msg=str(e)[:500])


async def process_revision(app, job: dict, revision_info: dict) -> None:
    job_id = job["id"]
    user_id = revision_info["user_id"]
    user_text = revision_info["user_text"]

    try:
        await update_status(job_id, "processing")
        await _update_progress(app, job_id, "analyzing")

        # Apply revision (loads transcript + current plan, calls LLM, saves new plan)
        cut_plan = await apply_revision(job_id, user_text)

        # Re-cut with new plan
        await _update_progress(app, job_id, "cutting")
        job_dir = TMP_DIR / job_id
        source = job_dir / "source.mp4"

        new_rev = await increment_revision(job_id)
        result = job_dir / f"result_v{new_rev}.mp4"
        cut_video(source, cut_plan, result)

        res_dur = get_result_duration(result)
        await update_status(job_id, "done", result_path=str(result), result_dur_s=res_dur)
        logger.info("Revision %d for job %s done", new_rev, job_id)

    except Exception as e:
        logger.exception("Revision for job %s failed: %s", job_id, e)
        await update_status(job_id, "error", error_msg=str(e)[:500])
