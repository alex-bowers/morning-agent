# Morning Agent

An agent that posts my daily morning updates to Slack.

These include:
- **Sports highlights** — checks if tracked teams played yesterday and posts YouTube highlight links.
- **Calendar summary** — fetches today's Google Calendar events and posts a summary.
- **Brain teaser** — picks a pre-generated teaser from a pool and posts it with the answer in a thread reply.

## Architecture

```
agent/
├── agent.py                    # Main agent — connects to MCP servers, calls Claude, posts to Slack
├── brain_teaser.py             # Pool management — load/save pool, get next teaser, memory/audit trail
├── generate_teaser_pool.py     # Batch generation script — generates ~90 teasers via Claude API
├── brain_teaser_memory.json    # Audit trail + cross-batch dedup history
├── teaser_pool.json            # Pre-generated pool of teasers (consumed daily)
├── credentials.json            # Google Calendar OAuth credentials
└── token.json                  # Google Calendar OAuth token

servers/
├── calendar_server.py          # MCP server for Google Calendar
└── sports_highlights_server.py # MCP server for YouTube sports highlights
```

## Setup

1. **Install dependencies:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure environment:**
   Copy `.env.example` to `.env` and fill in your values:
   ```bash
   cp .env.example .env
   ```

   Required: `ANTHROPIC_API_KEY`, Google Calendar credentials, Slack tokens, YouTube API key.

3. **Generate the teaser pool** (first time, and every ~3 months):
   ```bash
   .venv/bin/python3 agent/generate_teaser_pool.py
   ```

   Options:
   - `--count N` — generate N teasers instead of the default 90
   - `--dry-run` — print the prompt without calling Claude (for debugging)

4. **Run the agent:**
   ```bash
   .venv/bin/python3 agent/agent.py
   ```

## Brain Teaser System

The brain teaser uses a **pool-based architecture** to avoid repetition:

- **Pool** (`teaser_pool.json`) — A batch of ~90 pre-generated teasers, consumed one per day.
- **Memory** (`brain_teaser_memory.json`) — An audit trail recording every posted teaser, used for cross-batch deduplication.

### How it works

1. Every ~3 months, run `python agent/generate_teaser_pool.py` to create a fresh batch.
2. Each day, `agent.py` picks the next teaser from the pool (sequential, not random — ensures even distribution).
3. The teaser is posted to Slack with the answer in a thread reply.
4. The posted teaser is recorded in memory for future dedup.
5. When the pool runs low (≤5 remaining), a warning is logged.

### Batch generation

The generator:
- Distributes teasers across 6 categories, multiple sub-types, 50 themes, and 3 difficulty levels.
- Loads previously generated questions from memory to avoid cross-batch duplicates.
- Validates the output (category coverage, duplicate detection, field completeness).
- Normalizes Claude's category/sub-type names to match the canonical lists.

### Categories & sub-types

| Category | Sub-types |
|---|---|
| Riddle | classic object, nature, person/profession, time/abstract concept, double-meaning wordplay |
| Logic puzzle | grid/table deduction, truth-tellers & liars, ordering/sequencing, river-crossing/constraint, weighing/balance |
| Lateral thinking | detective-style scenario, paradox/counter-intuitive, strange situation, everyday object unexpected use |
| Maths puzzle | number sequence, combinatorics/counting, geometry/area, probability, algebra |
| Word puzzle | anagram, homophone, cryptic definition, wordplay letter manipulation, hidden word |
| Visual/spatial puzzle | matchstick/toothpick, folding & cutting paper, pattern continuation/odd-one-out, perspective, path/route |

### Difficulty distribution

Easy : Medium : Hard = 1 : 3 : 1

## Slack posting

- **Sports highlights** → `#sports-highlights` channel via webhook
- **Calendar summary** → `#general` channel via Bot API
- **Brain teaser** → `#general` channel via Bot API, with the answer in a thread reply
