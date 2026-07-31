"""
Brain Teaser Management
=======================
Handles brain teaser category/difficulty selection, memory tracking,
and history management to ensure variety across multiple runs.
"""

import json
import logging
import random
from datetime import date
from pathlib import Path

logger = logging.getLogger("morning_agent.brain_teaser")

MEMORY_FILE = Path(__file__).resolve().parent / "brain_teaser_memory.json"

MAX_HISTORY_ENTRIES = 100

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

DIFFICULTY_CYCLE = ["Hard", "Medium", "Medium", "Medium"]


def load_memory() -> dict:
    """
    Load brain teaser memory from the JSON file.
    Returns a fresh default state if the file doesn't exist yet.
    """
    if not MEMORY_FILE.exists():
        return {
            "recent_categories": [],
            "difficulty_cycle_position": 0,
            "history": [],
        }
    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_memory(memory: dict) -> None:
    """Save brain teaser memory to the JSON file (atomic write)."""
    tmp = MEMORY_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(memory, f, indent=2)
    tmp.replace(MEMORY_FILE)
    logger.info("Saved brain teaser memory to %s", MEMORY_FILE)


def pick_teaser_config(memory: dict) -> tuple[str, str, str]:
    """
    Select a brain teaser category, sub-type, and difficulty based on memory.
    Ensures variety by avoiding recently used categories and sub-types.

    Returns:
        tuple of (category, sub_type, difficulty)
    """
    recent = memory.get("recent_categories", [])
    available = [c for c in TEASER_CATEGORIES if c not in recent]

    if not available:
        available = TEASER_CATEGORIES

    category = random.choice(available)
    position = memory.get("difficulty_cycle_position", 0)
    difficulty = DIFFICULTY_CYCLE[position % len(DIFFICULTY_CYCLE)]

    recent_sub_types = memory.get("recent_sub_types", {}).get(category, [])
    all_sub_types = TEASER_SUB_TYPES[category]
    available_sub_types = [s for s in all_sub_types if s not in recent_sub_types]
    if not available_sub_types:
        available_sub_types = all_sub_types
    sub_type = random.choice(available_sub_types)

    logger.info(
        "Today's brain teaser: category='%s', sub_type='%s', difficulty='%s'",
        category, sub_type, difficulty,
    )
    logger.debug("Recent categories (excluded): %s", recent)
    logger.debug("Recent sub-types for '%s' (excluded): %s", category, recent_sub_types)

    return category, sub_type, difficulty


def update_memory(memory: dict, category: str, sub_type: str) -> dict:
    """
    Update memory after generating today's brain teaser.
    Tracks recently used categories/sub-types and advances the difficulty cycle.
    """
    recent = memory.get("recent_categories", [])
    recent.append(category)
    memory["recent_categories"] = recent[-3:]

    position = memory.get("difficulty_cycle_position", 0)
    memory["difficulty_cycle_position"] = (position + 1) % len(DIFFICULTY_CYCLE)

    recent_sub_types = memory.get("recent_sub_types", {})
    category_sub_types = recent_sub_types.get(category, [])
    category_sub_types.append(sub_type)
    recent_sub_types[category] = category_sub_types[-3:]
    memory["recent_sub_types"] = recent_sub_types

    history = memory.get("history", [])
    history.append({
        "date": str(date.today()),
        "category": category,
        "sub_type": sub_type,
        "difficulty": DIFFICULTY_CYCLE[position % len(DIFFICULTY_CYCLE)],
    })
    # Prune history to prevent unbounded growth
    memory["history"] = history[-MAX_HISTORY_ENTRIES:]

    return memory
