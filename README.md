# ClipCut

Telegram bot that downloads a video, transcribes it with AssemblyAI, identifies the best moments with Claude via OpenRouter, and delivers the cut clips directly in chat.

## Requirements

- Python 3.10+
- FFmpeg in PATH

## Setup

```bash
pip install -r requirements.txt
copy .env.example .env
# Edit .env and fill in all API keys
```

## Run

```bash
python main.py
```

## Cleanup (manual)

```bash
python cleanup.py
```

Removes job records and temp files older than 24 hours.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | — | BotFather token |
| `ASSEMBLYAI_API_KEY` | — | AssemblyAI API key (transcription) |
| `OPENROUTER_API_KEY` | — | OpenRouter API key (LLM analysis) |
| `LLM_PROVIDER` | `openrouter` | LLM provider: `openrouter` or `groq` |
| `MAX_FILE_SIZE_MB` | `500` | Max video file size in MB |
| `TMP_DIR` | `/tmp/clipcut` | Working directory for temporary files |
| `DB_PATH` | `clipcut.db` | SQLite database file path |
| `CLEANUP_AFTER_HOURS` | `24` | Hours before files and job records are deleted |
| `WHITELIST_CHAT_IDS` | (empty) | Comma-separated Telegram user IDs; empty = open access |

## Project layout

```
bot/
  handlers.py          Telegram ConversationHandler and all FSM states
  keyboards.py         Inline keyboards
  voice_handler.py     Voice message handlers
pipeline/
  downloader.py        Download file from Telegram
  transcriber.py       AssemblyAI transcription for video (word-level timestamps)
  voice_transcriber.py AssemblyAI transcription for voice messages (OGG → WAV → text)
  analyzer.py          LLM analysis: transcript → cut_plan JSON
  cutter.py            FFmpeg cutting and duration utilities
db/
  jobs.py              SQLite: jobs, users, feedback tables
prompts/
  cut_prompt.txt       System prompt for primary cut analysis
  revision_prompt.txt  System prompt for revision requests
config.py              Centralised settings from environment
worker.py              Background job processor
main.py                Entry point
cleanup.py             Temp file and DB cleanup utility
```
