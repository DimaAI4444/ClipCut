import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN: str = os.environ["TELEGRAM_TOKEN"]
ASSEMBLYAI_API_KEY: str = os.environ["ASSEMBLYAI_API_KEY"]
OPENROUTER_API_KEY: str = os.environ["OPENROUTER_API_KEY"]
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "openrouter")

MAX_FILE_SIZE_MB: int = int(os.getenv("MAX_FILE_SIZE_MB", "500"))
MAX_FILE_SIZE_BYTES: int = MAX_FILE_SIZE_MB * 1024 * 1024

TMP_DIR: Path = Path(os.getenv("TMP_DIR", "/tmp/clipcut"))
TMP_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH: str = os.getenv("DB_PATH", "clipcut.db")

CLEANUP_AFTER_HOURS: int = int(os.getenv("CLEANUP_AFTER_HOURS", "24"))

_whitelist_raw = os.getenv("WHITELIST_CHAT_IDS", "")
WHITELIST_CHAT_IDS: set[int] = (
    {int(x.strip()) for x in _whitelist_raw.split(",") if x.strip()}
    if _whitelist_raw.strip()
    else set()
)

PROMPTS_DIR: Path = Path(__file__).parent / "prompts"

MAX_REVISIONS: int = 3
WORKER_POLL_INTERVAL: int = 5  # seconds
STATUS_POLL_INTERVAL: int = 3  # seconds
