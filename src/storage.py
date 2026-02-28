"""Duplicate prevention using a local JSON file."""

import json
import logging
from pathlib import Path

logger = logging.getLogger("email-monitor.storage")

STORAGE_FILE = Path(__file__).parent / "processed_ids.json"


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
    """Save processed message IDs to JSON file."""
    STORAGE_FILE.write_text(
        json.dumps(list(ids), indent=2),
        encoding="utf-8",
    )
    logger.debug("Saved %d processed ID(s) to %s", len(ids), STORAGE_FILE)


def is_processed(message_id: str) -> bool:
    """Check if a message ID has already been processed."""
    return message_id in load_processed()


def mark_processed(message_id: str) -> None:
    """Mark a message ID as processed and persist."""
    ids = load_processed()
    ids.add(message_id)
    save_processed(ids)
