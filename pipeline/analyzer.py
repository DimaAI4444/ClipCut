import json
import logging
import re
from pathlib import Path

from config import (
    LLM_PROVIDER,
    PROMPTS_DIR,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 2


def load_prompt(filename: str) -> str:
    path = PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8")


def safe_parse_json(raw: str) -> dict:
    """Try three strategies to extract valid JSON from LLM response."""
    # Strip markdown fences
    cleaned = raw.strip()
    for fence in ("```json", "```"):
        if cleaned.startswith(fence):
            cleaned = cleaned[len(fence):]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    # Attempt 1: direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Attempt 2: find first {...} block
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    raise ValueError(f"LLM returned invalid JSON: {raw[:300]}")


async def _call_groq(system: str, user: str) -> str:
    from groq import AsyncGroq
    from config import GROQ_API_KEY
    c = AsyncGroq(api_key=GROQ_API_KEY)
    response = await c.chat.completions.create(
        model="llama-3.3-70b-versatile",
        temperature=0.1,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return response.choices[0].message.content


async def _call_openrouter(system: str, user: str) -> str:
    import openai
    from config import OPENROUTER_API_KEY
    c = openai.AsyncOpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
    )
    response = await c.chat.completions.create(
        model="anthropic/claude-sonnet-4-5",
        temperature=0.1,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return response.choices[0].message.content


async def _call_llm(system: str, user: str) -> str:
    if LLM_PROVIDER == "openrouter":
        return await _call_openrouter(system, user)
    if LLM_PROVIDER == "groq":
        return await _call_groq(system, user)
    return await _call_groq(system, user)


async def _call_llm_with_retry(system: str, user: str) -> dict:
    for attempt in range(MAX_RETRIES + 1):
        raw = await _call_llm(system, user)
        try:
            return safe_parse_json(raw)
        except ValueError as e:
            logger.warning("Attempt %d: invalid JSON — %s", attempt + 1, e)
            if attempt < MAX_RETRIES:
                # Append strict instruction for next attempt
                user = user + "\n\nВажно: верни ТОЛЬКО JSON без пояснений и без markdown."
    raise RuntimeError("LLM failed to return valid JSON after retries")


async def analyze(transcript: dict, params: dict) -> dict:
    """Primary analysis: transcript → cut_plan."""
    system_prompt = load_prompt("cut_prompt.txt")
    user_message = (
        f"Транскрипт видео (формат Whisper, word-level timestamps):\n"
        f"{json.dumps(transcript.get('words', []), ensure_ascii=False, indent=2)}\n\n"
        f"Параметры нарезки:\n"
        f"- Целевой хронометраж: {params.get('target_duration', 'оптимальный')}\n"
        f"- Дополнительные пожелания: {params.get('notes', 'нет')}\n"
        f"- Длительность исходника: {params.get('original_duration', 'неизвестно')} сек"
    )
    logger.info("Calling LLM for primary analysis (provider=%s)", LLM_PROVIDER)
    result = await _call_llm_with_retry(system_prompt, user_message)
    logger.info("Analysis done: %d keep, %d remove", len(result.get("keep", [])), len(result.get("remove", [])))
    return result


async def apply_revision(job_id: str, user_text: str) -> dict:
    """Revision: load existing plan + transcript, apply user correction."""
    from config import TMP_DIR
    import json as _json

    job_dir = TMP_DIR / job_id
    transcript_path = job_dir / "transcript.json"
    transcript = _json.loads(transcript_path.read_text(encoding="utf-8"))

    # Find current plan version
    revision = 0
    for i in range(3, -1, -1):
        p = job_dir / f"cut_plan_v{i}.json"
        if p.exists():
            revision = i
            break
    current_plan = _json.loads((job_dir / f"cut_plan_v{revision}.json").read_text(encoding="utf-8"))

    system_prompt = load_prompt("revision_prompt.txt")
    user_message = (
        f"ТРАНСКРИПТ:\n{_json.dumps(transcript.get('words', []), ensure_ascii=False)}\n\n"
        f"ТЕКУЩИЙ ПЛАН НАРЕЗКИ:\n{_json.dumps(current_plan, ensure_ascii=False)}\n\n"
        f"ПРАВКА ПОЛЬЗОВАТЕЛЯ:\n{user_text}"
    )

    logger.info("Calling LLM for revision %d (job=%s)", revision + 1, job_id)
    result = await _call_llm_with_retry(system_prompt, user_message)

    # Save new version
    next_rev = revision + 1
    out_path = job_dir / f"cut_plan_v{next_rev}.json"
    out_path.write_text(_json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Saved cut_plan_v%d for job %s", next_rev, job_id)
    return result
