import asyncio
import logging
import subprocess
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

ASSEMBLYAI_BASE = "https://api.assemblyai.com/v2"


def extract_audio(source: Path, job_id: str) -> Path:
    """Extract 16kHz mono WAV from video using FFmpeg."""
    from config import TMP_DIR
    audio_path = TMP_DIR / job_id / "audio.wav"
    cmd = [
        "ffmpeg", "-y",
        "-i", str(source),
        "-vn",
        "-ar", "16000",
        "-ac", "1",
        str(audio_path),
    ]
    logger.info("Extracting audio: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg audio extraction failed:\n{result.stderr}")
    logger.info("Audio extracted to %s", audio_path)
    return audio_path


async def transcribe(audio_path: Path) -> dict:
    """Upload audio to AssemblyAI, poll until ready, return word-level timestamps."""
    from config import ASSEMBLYAI_API_KEY
    headers = {"authorization": ASSEMBLYAI_API_KEY}

    async with httpx.AsyncClient(timeout=300) as client:
        # Step 1 — upload file
        logger.info("Uploading audio to AssemblyAI...")
        with audio_path.open("rb") as f:
            upload_resp = await client.post(
                f"{ASSEMBLYAI_BASE}/upload",
                headers=headers,
                content=f.read(),
            )
        upload_url = upload_resp.json()["upload_url"]

        # Step 2 — start transcription
        transcript_resp = await client.post(
            f"{ASSEMBLYAI_BASE}/transcript",
            headers=headers,
            json={
                "audio_url": upload_url,
                "speech_models": ["universal-2"],
                "format_text": False,
            },
        )
        resp_json = transcript_resp.json()
        logger.error("AssemblyAI response: %s", resp_json)
        transcript_id = resp_json["id"]
        logger.info("Transcription started: %s", transcript_id)

        # Step 3 — poll until complete
        while True:
            poll = await client.get(
                f"{ASSEMBLYAI_BASE}/transcript/{transcript_id}",
                headers=headers,
            )
            data = poll.json()
            status = data["status"]

            if status == "completed":
                logger.info(
                    "Transcription complete: %d words",
                    len(data.get("words", [])),
                )
                return {
                    "words": [
                        {
                            "word": w["text"],
                            "start": w["start"] / 1000.0,
                            "end": w["end"] / 1000.0,
                        }
                        for w in data.get("words", [])
                    ],
                    "duration": data.get("audio_duration", 0),
                    "text": data.get("text", ""),
                }
            elif status == "error":
                raise RuntimeError(f"AssemblyAI error: {data.get('error')}")

            logger.info("Transcription status: %s — waiting...", status)
            await asyncio.sleep(3)


def get_video_duration(source: Path) -> float:
    """Return video duration in seconds via ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(source),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return 0.0
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0
