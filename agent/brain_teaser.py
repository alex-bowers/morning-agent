"""
Brain Teaser Management — Pool-Based System
=============================================
Manages a pre-generated pool of brain teasers that is created in batch
(every ~3 months) and consumed one per day. This eliminates repetition
because the batch generation can see all teasers at once and guarantee
no duplicates.

Key concepts:
  - **Pool** (`teaser_pool.json`): Pre-generated teasers consumed sequentially.
  - **Memory** (`brain_teaser_memory.json`): Audit trail + cross-batch dedup.
  - **Generation script** (`generate_teaser_pool.py`): Creates a new batch.

Daily usage (in agent.py):
  1. load_pool() → get_next_teaser(pool) → post to Slack → save_pool(pool)
  2. Record the posted teaser in memory for cross-batch dedup.

Batch generation (run separately):
  1. python agent/generate_teaser_pool.py
"""

import json
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger("morning_agent.brain_teaser")

AGENT_DIR = Path(__file__).resolve().parent

POOL_FILE = AGENT_DIR / "teaser_pool.json"
MEMORY_FILE = AGENT_DIR / "brain_teaser_memory.json"

MAX_HISTORY_ENTRIES = 200  # Keep enough for cross-batch dedup

# ---------------------------------------------------------------------------
# Category & sub-type definitions — used by the generation script
# ---------------------------------------------------------------------------

TEASER_CATEGORIES = [
    "Riddle",
    "Logic puzzle",
    "Lateral thinking",
    "Maths puzzle",
    "Word puzzle",
    "Visual/spatial puzzle (described in text)",
]

TEASER_SUB_TYPES = {
    "Riddle": [
        "classic object riddle",
        "nature riddle",
        "person or profession riddle",
        "time or abstract concept riddle",
        "double-meaning wordplay riddle",
    ],
    "Logic puzzle": [
        "grid/table deduction puzzle",
        "truth-tellers and liars puzzle",
        "ordering or sequencing puzzle",
        "weighing or balance puzzle",
        "river-crossing or constraint puzzle",
    ],
    "Lateral thinking": [
        "strange situation explained by a single key fact",
        "detective-style 'what happened?' scenario",
        "everyday object used in an unexpected way",
        "paradox or counter-intuitive outcome scenario",
        "ambiguous sentence or missing context puzzle",
    ],
    "Maths puzzle": [
        "algebra puzzle",
        "number theory or divisibility puzzle",
        "combinatorics or counting puzzle",
        "rate, ratio or proportion puzzle",
        "geometry or area puzzle",
    ],
    "Word puzzle": [
        "anagram challenge",
        "cryptic definition or double meaning",
        "wordplay based on letter manipulation (remove, reverse, insert)",
        "compound word or portmanteau puzzle",
        "homophone or homophones-in-context puzzle",
    ],
    "Visual/spatial puzzle (described in text)": [
        "shape counting or rearrangement puzzle",
        "matchstick or toothpick puzzle",
        "folding and cutting paper puzzle",
        "rotation or reflection puzzle",
        "pattern continuation or odd-one-out puzzle",
    ],
}

DIFFICULTY_DISTRIBUTION = {
    "Easy": 1,
    "Medium": 3,
    "Hard": 1,
}

TEASER_THEMES = [
    "space & astronomy",
    "ocean & marine life",
    "ancient history",
    "modern technology",
    "cooking & food",
    "music & instruments",
    "geography & travel",
    "medicine & health",
    "sports & athletics",
    "architecture & buildings",
    "literature & books",
    "mythology & folklore",
    "weather & climate",
    "animals & wildlife",
    "art & painting",
    "cinema & film",
    "fashion & clothing",
    "gardens & plants",
    "mountains & climbing",
    "inventions & discoveries",
    "languages & linguistics",
    "mathematics & numbers",
    "rivers & waterways",
    "deserts & extremes",
    "cities & urban life",
    "forests & woodland",
    "electricity & magnetism",
    "photography & optics",
    "railways & trains",
    "aviation & flight",
    "sailing & ships",
    "minerals & gemstones",
    "volcanoes & geology",
    "seasons & cycles",
    "textiles & weaving",
    "pottery & ceramics",
    "chess & board games",
    "card games & probability",
    "clocks & timekeeping",
    "bridges & engineering",
    "postal systems & communication",
    "currencies & trade",
    "constellations & navigation",
    "fire & energy",
    "ice & polar regions",
    "jungles & biodiversity",
    "chocolate & confectionery",
    "codes & cryptography",
    "dances & choreography",
]

# Minimum number of teasers remaining before we warn about regeneration
POOL_LOW_THRESHOLD = 5


# ---------------------------------------------------------------------------
# Pool operations
# ---------------------------------------------------------------------------

def load_pool() -> dict:
    """
    Load the teaser pool from the JSON file.
    Returns a default empty structure if the file doesn't exist yet.
    """
    if not POOL_FILE.exists():
        return {
            "batch_id": "",
            "generated_at": "",
            "next_index": 0,
            "teasers": [],
        }
    with open(POOL_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_pool(pool: dict) -> None:
    """Save the teaser pool to the JSON file (atomic write)."""
    tmp = POOL_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pool, f, indent=2, ensure_ascii=False)
    tmp.replace(POOL_FILE)
    logger.info("Saved teaser pool to %s (next_index=%d)", POOL_FILE, pool["next_index"])


def get_next_teaser(pool: dict) -> dict | None:
    """
    Return the next teaser from the pool and advance the index.
    Skips any teaser marked as ``skipped: true``.
    Returns None if the pool is exhausted.
    """
    teasers = pool.get("teasers", [])
    index = pool.get("next_index", 0)

    while index < len(teasers):
        teaser = teasers[index]
        pool["next_index"] = index + 1

        if teaser.get("skipped"):
            logger.info("Skipping teaser #%d (marked as skipped)", index)
            index = pool["next_index"]
            continue

        logger.info(
            "Selected teaser #%d: category='%s', sub_type='%s', difficulty='%s', theme='%s'",
            index, teaser.get("category"), teaser.get("sub_type"),
            teaser.get("difficulty"), teaser.get("theme"),
        )
        return teaser

    # Exhausted
    pool["next_index"] = len(teasers)
    logger.warning("Teaser pool is exhausted (index %d of %d)", index, len(teasers))
    return None


def pool_needs_regeneration(pool: dict) -> bool:
    """Return True if the pool is empty or nearly exhausted."""
    teasers = pool.get("teasers", [])
    remaining = len(teasers) - pool.get("next_index", 0)

    if not teasers:
        logger.warning("Teaser pool is empty — generation needed")
        return True

    if remaining <= POOL_LOW_THRESHOLD:
        logger.warning(
            "Teaser pool is low (%d remaining out of %d) — generation recommended",
            remaining, len(teasers),
        )
        return True

    return False


# ---------------------------------------------------------------------------
# Memory operations — audit trail & cross-batch dedup
# ---------------------------------------------------------------------------

def load_memory() -> dict:
    """
    Load brain teaser memory (audit trail) from the JSON file.
    Returns a fresh default state if the file doesn't exist yet.
    Backward-compatible: merges in defaults for any missing keys.
    """
    defaults = {
        "completed_batches": [],
        "history": [],
        # Legacy keys (kept for backward compatibility with old files)
        "recent_categories": [],
        "difficulty_cycle_position": 0,
        "recent_sub_types": {},
    }

    if not MEMORY_FILE.exists():
        return defaults

    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Merge defaults so new keys appear even in old files
    for key, default_val in defaults.items():
        data.setdefault(key, default_val)

    return data


def save_memory(memory: dict) -> None:
    """Save brain teaser memory to the JSON file (atomic write)."""
    tmp = MEMORY_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(memory, f, indent=2, ensure_ascii=False)
    tmp.replace(MEMORY_FILE)
    logger.info("Saved brain teaser memory to %s", MEMORY_FILE)


def record_teaser_in_history(memory: dict, teaser: dict) -> dict:
    """
    Append a consumed teaser to the audit history.
    Keeps the history capped at MAX_HISTORY_ENTRIES for cross-batch dedup.
    """
    history = memory.get("history", [])
    history.append({
        "date": str(date.today()),
        "category": teaser.get("category", ""),
        "sub_type": teaser.get("sub_type", ""),
        "difficulty": teaser.get("difficulty", ""),
        "theme": teaser.get("theme", ""),
        "question": teaser.get("question", ""),
        "answer": teaser.get("answer", ""),
    })
    memory["history"] = history[-MAX_HISTORY_ENTRIES:]
    return memory


def get_previous_questions(memory: dict, limit: int = 90) -> list[str]:
    """
    Return the most recent question texts from history, for use as a
    'do not repeat' list during batch generation.
    """
    history = memory.get("history", [])
    return [entry["question"] for entry in history[-limit:] if entry.get("question")]


def record_batch_completed(memory: dict, pool: dict) -> dict:
    """Record that a batch has been fully consumed (for audit trail)."""
    batches = memory.get("completed_batches", [])
    batches.append({
        "batch_id": pool.get("batch_id", "unknown"),
        "generated_at": pool.get("generated_at", ""),
        "teasers_used": pool.get("next_index", 0),
        "completed_at": str(date.today()),
    })
    memory["completed_batches"] = batches
    return memory
