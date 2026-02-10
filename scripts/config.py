"""Shared config, constants, logging, and database setup for claude-digest."""

import logging
import os
import sqlite3
from pathlib import Path

import yaml

# Paths
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DOCS_DIR = ROOT_DIR / "docs"
DIGESTS_DIR = DOCS_DIR / "digests"
PROMPTS_DIR = ROOT_DIR / "prompts"
SOURCES_PATH = DATA_DIR / "sources.yml"
STATE_DB_PATH = DATA_DIR / "state.db"

# Temp files (written between pipeline stages)
COLLECTED_ITEMS_PATH = ROOT_DIR / "collected_items.json"
CURATED_DIGEST_PATH = ROOT_DIR / "curated_digest.json"

# GitHub
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_API_BASE = "https://api.github.com"
REPO_OWNER = "dannyockilson"
REPO_NAME = "claude-digest"

# Claude API
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-5-20250929"

# Thresholds
RELEVANCE_THRESHOLD = 6
STAR_VELOCITY_DEFAULT_THRESHOLD = 500
TOPIC_MIN_STARS = 5
CONTENT_MAX_LENGTH = 10_000
CONTENT_EXTRACT_TIMEOUT = 15

# Rate limiting
SEQUENTIAL_API_DELAY = 0.5  # seconds between sequential Claude calls
BATCH_POLL_INTERVAL = 30  # seconds between batch status checks
BATCH_POLL_TIMEOUT = 600  # 10 minutes max wait for batch

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("claude-digest")


def load_sources() -> dict:
    """Load and return the sources.yml configuration."""
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_prompt(name: str) -> str:
    """Load a prompt template by name (without extension)."""
    path = PROMPTS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8")


def get_db() -> sqlite3.Connection:
    """Get a connection to the state database, creating tables if needed."""
    db = sqlite3.connect(str(STATE_DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    _init_db(db)
    return db


def _init_db(db: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS seen_items (
            url_hash TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            title TEXT,
            source_type TEXT,
            first_seen TEXT NOT NULL,
            last_digest TEXT
        );

        CREATE TABLE IF NOT EXISTS digest_runs (
            run_date TEXT PRIMARY KEY,
            items_collected INTEGER,
            items_included INTEGER,
            items_filtered INTEGER,
            status TEXT
        );

        CREATE TABLE IF NOT EXISTS star_snapshots (
            repo TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            star_count INTEGER NOT NULL,
            PRIMARY KEY (repo, snapshot_date)
        );

        CREATE TABLE IF NOT EXISTS docs_hashes (
            url TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            last_checked TEXT NOT NULL,
            last_changed TEXT
        );
    """)
    db.commit()


def github_headers() -> dict:
    """Return headers for GitHub API requests."""
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers
