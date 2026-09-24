"""Loads `.env` before the test session starts.

The core dataset now lives in Postgres (`core_engine/db.py`), not the
vendored JSON export, so `LEDGER_DATABASE_URL` has to be set for even the
deterministic tests to run -- every other entry point (`server.py`,
`agents.py`, `deploy.py`) already loads `.env` itself; pytest is the one
process that didn't need to before.
"""

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
