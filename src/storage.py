"""Duplicate prevention using a local JSON file with in-memory cache."""

import json
import logging
from pathlib import Path

logger = logging.getLogger("email-monitor.storage")

# Store data file in project root, not inside src/
STORAGE_FILE = Path(__file__).parent.parent / "processed_ids.json"
MAX_STORED_IDS = 500

# In-memory cache — avoids re-reading from disk on every check
_cache: set | None = None


def load_processed() -> set:
    """Load processed message IDs from JSON file."""
    if not STORAGE_FILE.exists():
        logger.debug("No storage file found at %s — starting fresh", STORAGE_FILE)
        return set()
    try:
        data = json.loads(STORAGE_FILE.read_text(encoding="utf-8"))
        ids = set(data)
        logger.debug("Loaded %d processed ID(s) from %s", len(ids), STORAGE_FILE)
        return ids
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(
            "Corrupted storage file %s: %s — starting fresh", STORAGE_FILE, e
        )
        return set()


def save_processed(ids: set) -> None:
    """Save processed message IDs to JSON file, capping at MAX_STORED_IDS."""
    id_list = list(ids)
    if len(id_list) > MAX_STORED_IDS:
        id_list = id_list[-MAX_STORED_IDS:]
        ids = set(id_list)
        logger.debug("Trimmed processed IDs to %d entries", MAX_STORED_IDS)
    STORAGE_FILE.write_text(
        json.dumps(id_list, indent=2),
        encoding="utf-8",
    )
    logger.debug("Saved %d processed ID(s) to %s", len(ids), STORAGE_FILE)


def is_processed(message_id: str) -> bool:
    """Check if a message ID has already been processed (cached)."""
    global _cache
    if _cache is None:
        _cache = load_processed()
    return message_id in _cache


def mark_processed(message_id: str) -> None:
    """Mark a message ID as processed, update cache, and persist."""
    global _cache
    if _cache is None:
        _cache = load_processed()
    _cache.add(message_id)
    save_processed(_cache)
