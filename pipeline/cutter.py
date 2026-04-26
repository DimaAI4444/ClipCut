import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def cut_video(source: Path, cut_plan: dict, output: Path) -> None:
    """Cut video according to cut_plan['keep'] segments using FFmpeg concat filter."""
    segments = cut_plan.get("keep", [])
    if not segments:
        raise ValueError("cut_plan has no 'keep' segments")

    inputs = []
    filter_parts = []

    for i, seg in enumerate(segments):
        start = float(seg["start"])
        end = float(seg["end"])
        inputs += ["-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(source)]
        filter_parts.append(f"[{i}:v][{i}:a]")

    n = len(segments)
    filter_str = "".join(filter_parts) + f"concat=n={n}:v=1:a=1[vout][aout]"

    cmd = (
        ["ffmpeg", "-y"]
        + inputs
        + [
            "-filter_complex", filter_str,
            "-map", "[vout]",
            "-map", "[aout]",
            "-avoid_negative_ts", "make_zero",
            str(output),
        ]
    )

    logger.info("FFmpeg cut: %d segments → %s", n, output)
    logger.debug("FFmpeg cmd: %s", " ".join(cmd))

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("FFmpeg stderr: %s", result.stderr[-2000:])
        raise RuntimeError(f"FFmpeg failed (exit {result.returncode}):\n{result.stderr[-1000:]}")

    logger.info("FFmpeg done: output=%s (%.1f MB)", output, output.stat().st_size / 1024 / 1024)


def get_result_duration(result_path: Path) -> float:
    """Return duration of result video in seconds."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(result_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def format_duration(seconds: float) -> str:
    """Format seconds as m:ss."""
    if seconds <= 0:
        return "0:00"
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}:{s:02d}"
