import uuid
import aiosqlite
from config import DB_PATH


# ---------------------------------------------------------------------------
# Schema init
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    file_path       TEXT,
    result_path     TEXT,
    error_msg       TEXT,
    revision        INTEGER DEFAULT 0,
    target_duration TEXT,
    user_notes      TEXT,
    original_dur_s  REAL,
    result_dur_s    REAL,
    progress_msg_id INTEGER
);

CREATE TABLE IF NOT EXISTS users (
    user_id         INTEGER PRIMARY KEY,
    username        TEXT,
    first_seen      DATETIME DEFAULT CURRENT_TIMESTAMP,
    total_jobs      INTEGER DEFAULT 0,
    is_whitelisted  INTEGER DEFAULT 0,
    plan            TEXT DEFAULT 'free',
    plan_expires    DATETIME
);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    job_id      TEXT,
    rating      INTEGER,
    text        TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA_SQL)
        await db.commit()


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

async def create_job(
    user_id: int,
    file_path: str,
    target_duration: str = "",
    notes: str = "",
) -> str:
    job_id = str(uuid.uuid4())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO jobs (id, user_id, status, file_path, target_duration, user_notes)
            VALUES (?, ?, 'queued', ?, ?, ?)
            """,
            (job_id, user_id, file_path, target_duration, notes),
        )
        await db.commit()
    return job_id


async def get_job(job_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_next_queued_job() -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def update_status(
    job_id: str,
    status: str,
    result_path: str | None = None,
    error_msg: str | None = None,
    result_dur_s: float | None = None,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE jobs
            SET status=?, result_path=?, error_msg=?, result_dur_s=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (status, result_path, error_msg, result_dur_s, job_id),
        )
        await db.commit()


async def update_progress_msg_id(job_id: str, msg_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE jobs SET progress_msg_id=? WHERE id=?",
            (msg_id, job_id),
        )
        await db.commit()


async def get_progress_msg_id(job_id: str) -> tuple[int, int] | None:
    """Returns (user_id, progress_msg_id) or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, progress_msg_id FROM jobs WHERE id=?", (job_id,)
        ) as cur:
            row = await cur.fetchone()
            if row and row["progress_msg_id"]:
                return row["user_id"], row["progress_msg_id"]
    return None


async def set_original_duration(job_id: str, dur_s: float) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE jobs SET original_dur_s=? WHERE id=?", (dur_s, job_id)
        )
        await db.commit()


async def increment_revision(job_id: str) -> int:
    """Increments revision counter and returns new value."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE jobs SET revision=revision+1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (job_id,),
        )
        await db.commit()
        async with db.execute("SELECT revision FROM jobs WHERE id=?", (job_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_current_revision(job_id: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT revision FROM jobs WHERE id=?", (job_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_latest_job_for_user(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def mark_archived(job_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE jobs SET status='archived', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (job_id,),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def upsert_user(user_id: int, username: str | None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, username) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username
            """,
            (user_id, username),
        )
        await db.commit()


async def increment_user_jobs(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET total_jobs=total_jobs+1 WHERE user_id=?", (user_id,)
        )
        await db.commit()


async def is_whitelisted(user_id: int) -> bool:
    from config import WHITELIST_CHAT_IDS
    if not WHITELIST_CHAT_IDS:
        return True  # whitelist empty = open access
    if user_id in WHITELIST_CHAT_IDS:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT is_whitelisted FROM users WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return bool(row and row[0])


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

async def save_feedback(user_id: int, job_id: str | None, text: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO feedback (user_id, job_id, text) VALUES (?, ?, ?)",
            (user_id, job_id, text),
        )
        await db.commit()
