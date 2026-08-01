"""
Brain Teaser Pool Generator
============================
Generates a batch of brain teasers using Claude, with guaranteed variety
across categories, sub-types, difficulties, and themes. Includes past
questions from memory to avoid cross-batch duplication.

Usage:
    python agent/generate_teaser_pool.py            # Generate 90 teasers (default)
    python agent/generate_teaser_pool.py --count 30 # Generate 30 teasers
    python agent/generate_teaser_pool.py --dry-run  # Show prompt without calling Claude
"""

import argparse
import json
import logging
import os
import sys
from datetime import date

import anthropic
from brain_teaser import (
    AGENT_DIR,
    DIFFICULTY_DISTRIBUTION,
    POOL_FILE,
    TEASER_CATEGORIES,
    TEASER_SUB_TYPES,
    TEASER_THEMES,
    get_previous_questions,
    load_memory,
    load_pool,
    save_pool,
)
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("morning_agent.generate_pool")

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")

DEFAULT_COUNT = 90


def build_distribution_spec(count: int) -> str:
    """
    Build a human-readable specification of how many teasers of each
    category/sub-type/difficulty to generate, aiming for even distribution.
    """
    total_weight = sum(DIFFICULTY_DISTRIBUTION.values())
    lines = []

    # Calculate how many per category (as even as possible)
    per_category = count // len(TEASER_CATEGORIES)
    remainder = count % len(TEASER_CATEGORIES)

    for i, category in enumerate(TEASER_CATEGORIES):
        cat_count = per_category + (1 if i < remainder else 0)
        sub_types = TEASER_SUB_TYPES[category]

        # Distribute across sub-types
        per_sub = cat_count // len(sub_types)
        sub_remainder = cat_count % len(sub_types)

        sub_breakdown = []
        for j, sub in enumerate(sub_types):
            n = per_sub + (1 if j < sub_remainder else 0)
            if n > 0:
                sub_breakdown.append(f"    - {n}x {sub}")

        # Distribute across difficulties
        diff_parts = []
        for diff, weight in DIFFICULTY_DISTRIBUTION.items():
            n = round(cat_count * weight / total_weight)
            if n > 0:
                diff_parts.append(f"{n} {diff}")

        lines.append(
            f"  {category}: {cat_count} teasers (difficulty mix: {', '.join(diff_parts)})\n" + "\n".join(sub_breakdown)
        )

    return "\n".join(lines)


def build_prompt(count: int, previous_questions: list[str]) -> str:
    """Build the generation prompt for Claude."""
    distribution = build_distribution_spec(count)

    themes_list = ", ".join(TEASER_THEMES)

    previous_block = ""
    if previous_questions:
        numbered = "\n".join(f"  {i + 1}. {q}" for i, q in enumerate(previous_questions))
        previous_block = (
            "\n\nIMPORTANT — Do NOT repeat or closely resemble any of these "
            "previously generated questions:\n" + numbered
        )

    prompt = (
        f"Generate exactly {count} diverse brain teasers for a daily puzzle subscription. "
        "Each teaser must have a unique question — no duplicates or near-duplicates within "
        "the batch or with previously generated questions.\n\n"
        "DISTRIBUTION TARGETS (approximately):\n"
        f"{distribution}\n\n"
        f"THEMES — Assign a unique theme to each teaser from this list (rotate through them, "
        f"don't repeat a theme within the same category): {themes_list}\n\n"
        "QUALITY REQUIREMENTS:\n"
        "- Each question must be genuinely puzzling and original — avoid well-known riddles "
        "or puzzles that appear in common collections\n"
        "- Each answer must be clear, unambiguous, and satisfying\n"
        "- Questions should be self-contained — no external knowledge beyond general "
        "awareness is required\n"
        "- For visual/spatial puzzles, describe the setup clearly in text so the reader "
        "can visualise it\n"
        "- Difficulty should match the label: Easy puzzles should be solvable in under "
        "a minute; Medium in 2-5 minutes; Hard in 5-15 minutes\n\n"
        "OUTPUT FORMAT — Return ONLY a JSON array. Each element must be an object with "
        "these exact keys:\n"
        '  - "id": integer starting at 1\n'
        '  - "category": one of the categories listed above\n'
        '  - "sub_type": one of the sub-types for that category\n'
        '  - "difficulty": "Easy", "Medium", or "Hard"\n'
        '  - "theme": one of the themes from the theme list\n'
        '  - "question": the puzzle question text\n'
        '  - "answer": the answer text\n\n'
        "Return ONLY the JSON array — no markdown fences, no commentary, no preamble."
        f"{previous_block}"
    )

    return prompt


def validate_teasers(teasers: list[dict], count: int) -> list[str]:
    """Validate the generated teasers and return a list of warning strings."""
    warnings = []

    if len(teasers) != count:
        warnings.append(f"Expected {count} teasers, got {len(teasers)}")

    required_keys = {"id", "category", "sub_type", "difficulty", "theme", "question", "answer"}
    for i, t in enumerate(teasers):
        missing = required_keys - set(t.keys())
        if missing:
            warnings.append(f"Teaser #{i + 1} missing keys: {missing}")

    # Check categories are represented
    categories_seen = set()
    for t in teasers:
        if "category" in t:
            categories_seen.add(t["category"])
    missing_cats = set(TEASER_CATEGORIES) - categories_seen
    if missing_cats:
        warnings.append(f"Missing categories: {missing_cats}")

    # Check for duplicate questions
    questions = [t.get("question", "") for t in teasers]
    dupes = len(questions) - len(set(questions))
    if dupes > 0:
        warnings.append(f"Found {dupes} duplicate question(s)")

    # Check categories are valid
    for t in teasers:
        cat = t.get("category", "")
        if cat not in TEASER_CATEGORIES:
            warnings.append(f"Unknown category: '{cat}'")
        elif t.get("sub_type", "") not in TEASER_SUB_TYPES.get(cat, []):
            warnings.append(f"Unknown sub_type '{t.get('sub_type')}' for category '{cat}'")

    # Check difficulties
    for t in teasers:
        diff = t.get("difficulty", "")
        if diff not in ("Easy", "Medium", "Hard"):
            warnings.append(f"Unknown difficulty: '{diff}'")

    return warnings


def generate_pool(count: int, dry_run: bool = False) -> None:
    """Generate a new teaser pool and save it."""
    logger.info("=== Teaser Pool Generator ===")
    logger.info("Generating %d teasers (dry_run=%s)", count, dry_run)

    # Check for existing pool
    existing_pool = load_pool()
    if existing_pool.get("teasers") and existing_pool.get("next_index", 0) < len(existing_pool["teasers"]):
        remaining = len(existing_pool["teasers"]) - existing_pool["next_index"]
        logger.warning(
            "Existing pool still has %d teaser(s) remaining (batch_id=%s). Generating a new pool will replace it.",
            remaining,
            existing_pool.get("batch_id", "unknown"),
        )

    # Load memory for cross-batch dedup
    memory = load_memory()
    previous_questions = get_previous_questions(memory, limit=90)
    logger.info("Loaded %d previous questions for dedup", len(previous_questions))

    prompt = build_prompt(count, previous_questions)

    if dry_run:
        logger.info("=== DRY RUN — prompt that would be sent to Claude ===")
        print(prompt)
        return

    # Call Claude
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    logger.info("Sending generation prompt to Claude (%d chars)…", len(prompt))
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=16384,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = next(
        (block.text for block in response.content if block.type == "text"),
        "",
    )

    # Save raw response for debugging
    debug_file = AGENT_DIR / "teaser_pool_raw_response.json"
    with open(debug_file, "w", encoding="utf-8") as f:
        f.write(raw_text)
    logger.info("Saved raw Claude response to %s (%d chars)", debug_file, len(raw_text))

    # Parse JSON — strip markdown fences if Claude adds them
    text = raw_text.strip()
    if text.startswith("```"):
        # Remove opening fence (e.g. ```json)
        first_newline = text.index("\n")
        text = text[first_newline + 1:]  # fmt: skip
        # Remove closing fence
        text = text.rsplit("```", 1)[0]

    try:
        teasers = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse Claude's response as JSON: %s", e)
        logger.error("Raw response (first 500 chars): %s", raw_text[:500])
        logger.error("Full raw response saved to %s for inspection", debug_file)
        sys.exit(1)

    if not isinstance(teasers, list):
        logger.error("Expected a JSON array, got %s", type(teasers).__name__)
        sys.exit(1)

    # Normalize categories — Claude sometimes shortens them
    CATEGORY_ALIASES = {
        "Visual/spatial puzzle": "Visual/spatial puzzle (described in text)",
        "Visual puzzle": "Visual/spatial puzzle (described in text)",
        "Spatial puzzle": "Visual/spatial puzzle (described in text)",
    }
    # Normalize sub-types — Claude sometimes drops the parenthetical detail
    SUB_TYPE_ALIASES = {
        "wordplay based on letter manipulation": "wordplay based on letter manipulation (remove, reverse, insert)",
    }
    for t in teasers:
        cat = t.get("category", "")
        if cat in CATEGORY_ALIASES:
            t["category"] = CATEGORY_ALIASES[cat]
        sub = t.get("sub_type", "")
        if sub in SUB_TYPE_ALIASES:
            t["sub_type"] = SUB_TYPE_ALIASES[sub]

    # Validate
    warnings = validate_teasers(teasers, count)
    for w in warnings:
        logger.warning("Validation: %s", w)

    if warnings:
        logger.warning(
            "Pool generated with %d warning(s). Review before using.",
            len(warnings),
        )
    else:
        logger.info("All validation checks passed ✓")

    # Build the pool
    batch_id = f"{date.today().strftime('%Y-Q')}{(date.today().month - 1) // 3 + 1}"
    pool = {
        "batch_id": batch_id,
        "generated_at": str(date.today()),
        "next_index": 0,
        "teasers": teasers,
    }

    save_pool(pool)
    logger.info(
        "✓ Saved %d teasers to %s (batch_id=%s)",
        len(teasers),
        POOL_FILE,
        batch_id,
    )

    # Print summary
    category_counts = {}
    for t in teasers:
        cat = t.get("category", "unknown")
        category_counts[cat] = category_counts.get(cat, 0) + 1

    logger.info("Category distribution: %s", category_counts)

    difficulty_counts = {}
    for t in teasers:
        diff = t.get("difficulty", "unknown")
        difficulty_counts[diff] = difficulty_counts.get(diff, 0) + 1

    logger.info("Difficulty distribution: %s", difficulty_counts)


def main():
    parser = argparse.ArgumentParser(description="Generate a new batch of brain teasers for the daily pool.")
    parser.add_argument(
        "--count",
        "-n",
        type=int,
        default=DEFAULT_COUNT,
        help=f"Number of teasers to generate (default: {DEFAULT_COUNT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the prompt without calling Claude",
    )
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY and not args.dry_run:
        logger.error("ANTHROPIC_API_KEY not set. Add it to .env or set the environment variable.")
        sys.exit(1)

    generate_pool(count=args.count, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
