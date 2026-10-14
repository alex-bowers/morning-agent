# Morning Agent

An AI-powered daily briefing agent that posts morning updates to Slack. It uses **MCP (Model Context Protocol) servers** to give Claude real-time access to calendars and sports data, then formats and routes the results to the right Slack channels.

**What it posts each morning:**

- **Sports highlights** — Checks if tracked teams played yesterday and posts YouTube highlight links (no spoilers).
- **Calendar summary** — Fetches today's Google Calendar events and posts a clear summary.
- **Brain teaser** — Picks a pre-generated teaser from a pool and posts the question, with the answer hidden in a Slack thread reply.
- **Italian football headlines** — Fetches recent Italian-language calcio headlines from Google News RSS and posts them with English translations in a Slack thread reply.
- **Wishlist price watch** — Checks a public Amazon UK wishlist and posts any items priced under a target (default £1.00). Posts nothing when no items are under target.

## How it works

### Data flow

```
┌─────────────────────────────────────────────────────────────────┐
│                        agent.py (main)                          │
│                                                                 │
│  1. Load next brain teaser from pool                            │
│  2. Spin up three MCP servers (calendar + sports + Italian) via stdio │
│  3. Send a prompt to Claude with MCP tools available            │
│  4. Claude calls MCP tools → gets real-time data                │
│  5. Claude returns a response with ##SPORTS## / ##CALENDAR## / ##ITALIAN## │
│  6. Parse sections → post to Slack channels                     │
│  7. Post brain teaser (question + thread reply with answer)     │
│  8. Record teaser in memory, save pool state                    │
│  9. Check wishlist prices → post items under target             │
└─────────────────────────────────────────────────────────────────┘
```

### Agent loop (Claude + MCP tools)

The agent connects to three MCP servers over stdio and presents their tools to Claude. Claude decides which tools to call based on the user prompt:

1. **Prompt sent to Claude** — The prompt asks Claude to check sports results, calendar events, and Italian football headlines, and to format the response using `##SPORTS##`, `##CALENDAR##`, and `##ITALIAN##` section markers.
2. **Tool discovery** — All three MCP servers advertise their tools. The agent builds a routing map so each tool call goes to the right server.
3. **Tool call loop** — Claude may call tools multiple times (e.g. `sports_get_highlights`, `calendar_get_todays_events`, `italian_get_headlines`). The agent routes each call to the correct MCP session and returns the result.
4. **Final response** — When Claude finishes (`stop_reason == "end_turn"`), the agent extracts the text and parses it into sections using the markers.
5. **Slack posting** — Each section is posted to its designated channel.

### Section routing

| Section marker | Slack channel | Posting method |
|---|---|---|
| `##SPORTS##` | `#sports-highlights` | Incoming webhook (`SLACK_WEBHOOK_SPORTS`) |
| `##CALENDAR##` | `#calendar` | Bot API (`SLACK_BOT_TOKEN`) |
| `##ITALIAN##` | `#italian-football` | Bot API with thread reply (Italian headlines + English translations) |
| Brain teaser | `#brain-teaser` | Bot API with thread reply |
| Wishlist price watch | `#wishlist` | Bot API (only when items are under target) |

## Project structure

```
agent/
├── agent.py                    # Main agent — MCP client loop, Claude orchestration, Slack posting
├── brain_teaser.py             # Pool management — load/save pool, get next teaser, memory/audit trail
├── price_watch.py              # Wishlist price watch — fetch Amazon wishlist, find items under target
├── generate_teaser_pool.py     # Batch generation script — generates ~60 teasers via Claude API
├── brain_teaser_memory.json    # Audit trail + cross-batch dedup history (auto-managed)
├── teaser_pool.json            # Pre-generated pool of teasers (consumed daily, auto-managed)
├── credentials.json            # Google Calendar OAuth credentials (you create this)
└── token.json                  # Google Calendar OAuth token (auto-generated on first auth)

servers/
├── calendar_server.py          # MCP server — exposes Google Calendar tools
├── italian_headlines_server.py # MCP server — exposes Italian football headline tools
└── sports_highlights_server.py # MCP server — exposes ESPN + YouTube sports tools
```

## MCP servers

### Calendar server (`servers/calendar_server.py`)

Exposes Google Calendar data through MCP tools. Authenticates with OAuth2 (opens a browser on first run, then caches the token).

**Tools:**

| Tool | Description |
|---|---|
| `calendar_get_todays_events` | Fetches today's events from primary and shared calendars, merged and sorted by start time. Returns all-day and timed events. |
| `calendar_get_stats` | Returns metadata about which calendars are configured. |

**Data sources:**
- Primary Google Calendar (personal)
- Shared Google Calendar (configured via `GOOGLE_SHARED_CALENDAR_ID`)

**Timezone:** Defaults to `Europe/London`, overridable via `LOCAL_TIMEZONE` env var.

### Sports highlights server (`servers/sports_highlights_server.py`)

Checks whether tracked teams played and finds YouTube highlight videos. Uses two data sources:

1. **ESPN unofficial API** — Checks game scores for yesterday's date. Caches responses by league to avoid duplicate calls when multiple teams share a league.
2. **YouTube Data API v3** — Searches for highlight videos, filters by minimum duration (5 minutes default), and scores results to prefer official league/team channels over fan content.

**Tools:**

| Tool | Description |
|---|---|
| `sports_get_highlights` | Gets yesterday's game results and YouTube highlight URLs for all tracked teams (or a specific team). |
| `sports_get_teams_that_played` | Quick check for which tracked teams had games yesterday. |
| `sports_get_dataset_stats` | Overview of which teams and leagues are covered. |

**Tracked teams:**

| Team | League | ESPN sport/league |
|---|---|---|
| New England Patriots | NFL | `football/nfl` |
| Boston Celtics | NBA | `basketball/nba` |
| Boston Bruins | NHL | `hockey/nhl` |
| Boston Red Sox | MLB | `baseball/mlb` |
| New England Revolution | MLS | `soccer/usa.1` |
| Palermo F.C. | Serie B | `soccer/ita.2` |

**YouTube scoring system** — Results are ranked by:
- **+3** for official league channels (NBA, NFL, NHL, MLB, MLS, Serie B)
- **+2** for team's own channel
- **+1** for titles containing "full" or "extended"
- **−1** for titles containing "reaction" or "fan"

Both APIs use exponential backoff retries (3 attempts) on server errors.

### Italian headlines server (`servers/italian_headlines_server.py`)

Fetches recent Italian-language football (calcio) headlines for language learners. Uses Google News RSS — no API key needed.

**Data source:**
- Google News RSS (`https://news.google.com/rss/search?q=calcio+serie+A&hl=it&gl=IT&ceid=IT:it`) — returns headlines from Italian publications like Gazzetta dello Sport, Sky Sport, Corriere dello Sport, Tuttosport, etc.

**Tools:**

| Tool | Description |
|---|---|
| `italian_get_headlines` | Fetches recent Italian calcio headlines (default: 3). Returns title, source, URL, and publication date for each. |
| `italian_get_stats` | Returns metadata about the data source and configuration. |

**Slack posting:** The Italian section is split — Italian headlines go in the main message, English translations (lines prefixed with `EN:`) go in a thread reply. This mirrors the brain teaser pattern (question in main message, answer in thread).

The RSS feed uses exponential backoff retries (3 attempts) on server errors.

## Wishlist price watch

`agent/price_watch.py` checks a public Amazon UK wishlist once per run and reports every item priced below a single target price. It runs after the brain teaser and does not use Claude or MCP — comparing prices needs no reasoning.

### How it works

1. Fetches `https://www.amazon.co.uk/hz/wishlist/ls/<WISHLIST_ID>` with `httpx`.
2. Follows the wishlist's "show more" URL (the request the browser makes as you scroll) until Amazon includes its end-of-list marker. A cap of 20 pages stops runaway pagination.
3. Extracts each item's ASIN (Amazon product ID), title and current price.
4. Posts every item priced strictly below `WISHLIST_TARGET_PRICE` to `#wishlist`, cheapest first, with a link to each product.

No state is kept between runs, so an item is posted every day while its price stays below the target. Nothing is posted when no items are below the target. Items without a current price (for example, unavailable items) are skipped and logged.

### Requirements

- The wishlist must be public (shared as "Public" in Amazon's list settings).
- The Slack bot must be a member of the `#wishlist` channel.
- The feature is skipped unless both `WISHLIST_ID` and `SLACK_CHANNEL_WISHLIST` are set.

### Limitations

This reads Amazon's public web page rather than an official API, so it can break if Amazon changes the page layout or starts blocking requests. Amazon's terms of use restrict automated access; one check a day of your own list keeps the load minimal. When the check fails, a short failure message is posted to `#wishlist` rather than failing silently.

## Brain teaser system

The brain teaser uses a **pool-based architecture** to avoid repetition:

- **Pool** (`teaser_pool.json`) — A batch of ~60 pre-generated teasers, consumed one per day.
- **Memory** (`brain_teaser_memory.json`) — An audit trail recording every posted teaser, used for cross-batch deduplication (keeps up to 200 entries).

### How it works

1. Every ~2 months, run `python agent/generate_teaser_pool.py` to create a fresh batch.
2. Each day, `agent.py` picks the next teaser from the randomly ordered pool.
3. The question is posted to `#brain-teaser`, with the answer in a thread reply.
4. The posted teaser is recorded in memory for future dedup.
5. When the pool runs low (≤5 remaining), a warning is logged.
6. Skipped teasers (marked `"skipped": true`) are automatically bypassed.

### Batch generation

The generator (`generate_teaser_pool.py`):

1. Loads previously generated questions from memory to avoid cross-batch duplicates.
2. Builds a distribution spec that spreads teasers across 6 categories, their sub-types, 50 themes, and 2 difficulty levels (Medium : Hard = 3 : 1).
3. Sends the prompt to Claude and requests a JSON array of teaser objects.
4. Normalizes Claude's category/sub-type names to match the canonical lists (e.g. "Visual puzzle" → "Visual/spatial puzzle (described in text)").
5. Validates the output — checks count, required fields, category coverage, duplicate detection, and valid difficulty labels.
6. Randomises the order and saves the pool with a batch ID and timestamp.

**Teaser object format:**

```json
{
  "id": 1,
  "category": "Logic puzzle",
  "sub_type": "truth-tellers and liars puzzle",
  "difficulty": "Medium",
  "theme": "space & astronomy",
  "question": "On the planet Verax…",
  "answer": "The guard on the left…"
}
```

### Categories & sub-types

| Category | Sub-types |
|---|---|
| Riddle | classic object, nature, person/profession, time/abstract concept, double-meaning wordplay |
| Logic puzzle | grid/table deduction, truth-tellers & liars, ordering/sequencing, river-crossing/constraint, weighing/balance |
| Lateral thinking | strange situation explained by a single key fact, detective-style "what happened?", everyday object unexpected use, paradox/counter-intuitive, ambiguous sentence or missing context |
| Maths puzzle | algebra, number theory/divisibility, combinatorics/counting, rate/ratio/proportion, geometry/area |
| Word puzzle | anagram, cryptic definition or double meaning, wordplay letter manipulation (remove, reverse, insert), compound word/portmanteau, homophone |
| Visual/spatial puzzle (described in text) | shape counting/rearrangement, matchstick/toothpick, folding & cutting paper, rotation/reflection, pattern continuation/odd-one-out |

### Difficulty distribution

Medium : Hard = 3 : 1

### Themes

50 themes cycle through the teasers for variety — including space, ocean, ancient history, modern technology, cooking, music, mythology, cryptography, and many more.

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

   | Variable | Required | Description |
   |---|---|---|
   | `ANTHROPIC_API_KEY` | ✅ | Anthropic API key for Claude |
   | `ANTHROPIC_MODEL` | No | Claude model to use (default: `claude-sonnet-4-5-20250929`) |
   | `GOOGLE_CREDENTIALS_PATH` | ✅ | Absolute path to `credentials.json` from Google Cloud Console |
   | `GOOGLE_SHARED_CALENDAR_ID` | No | Shared calendar ID (fetches primary calendar regardless) |
   | `SLACK_BOT_TOKEN` | ✅ | Slack Bot OAuth token (`xoxb-...`) for calendar & brain teaser channels |
   | `SLACK_CHANNEL_CALENDAR` | ✅ | Channel ID for calendar posts (e.g. `C08XXXXXXXXX`) |
   | `SLACK_CHANNEL_BRAIN_TEASER` | ✅ | Channel ID for brain teaser posts |
   | `SLACK_CHANNEL_ITALIAN` | ✅ | Channel ID for Italian football headlines |
   | `SLACK_WEBHOOK_SPORTS` | ✅ | Incoming webhook URL for sports highlights channel |
   | `YOUTUBE_API_KEY` | ✅ | YouTube Data API v3 key |
   | `LOCAL_TIMEZONE` | No | Timezone for calendar events (default: `Europe/London`) |
   | `MIN_DURATION_SECONDS` | No | Minimum YouTube video length in seconds (default: `300`) |
   | `ITALIAN_NUM_HEADLINES` | No | Number of Italian headlines to fetch (default: `3`) |
   | `WISHLIST_ID` | No | Amazon UK wishlist ID — the code after `/hz/wishlist/ls/` in the list URL. Enables the price watch. |
   | `SLACK_CHANNEL_WISHLIST` | No | Channel ID for wishlist price alerts. Enables the price watch. |
   | `WISHLIST_TARGET_PRICE` | No | Post items priced below this amount in pounds (default: `1.00`) |

3. **Google Calendar OAuth:**
   - Download `credentials.json` from the [Google Cloud Console](https://console.cloud.google.com/) with the Calendar read-only scope.
   - Place it at the path specified by `GOOGLE_CREDENTIALS_PATH`.
   - On first run, the calendar server opens a browser for OAuth consent and saves `token.json` automatically.

4. **Generate the teaser pool** (first time, and every ~2 months):
   ```bash
   .venv/bin/python3 agent/generate_teaser_pool.py
   ```

   Options:
   - `--count N` — generate N teasers instead of the default 60
   - `--dry-run` — print the prompt without calling Claude (for debugging)

5. **Run the agent:**
   ```bash
   .venv/bin/python3 agent/agent.py
   ```

## Slack posting

| Channel | Content | Method |
|---|---|---|
| `#sports-highlights` | Yesterday's game results + YouTube highlights | Incoming webhook |
| `#calendar` | Today's calendar events summary | Bot API |
| `#brain-teaser` | Daily brain teaser (answer in thread reply) | Bot API |
| `#italian-football` | Italian calcio headlines (English translations in thread reply) | Bot API with thread reply |
| `#wishlist` | Wishlist items under the target price (nothing when none are) | Bot API |

## Logs

When the agent runs on its launchd schedule, output is written to two files in `agent/`:

| File | Contents |
|---|---|
| `agent.log` | The full log: every INFO, WARNING and ERROR line from the agent and the MCP servers |
| `agent_error.log` | Warnings, errors and tracebacks from the agent only — check this first when a run fails |

Warnings from the MCP servers appear only in `agent.log`. When you run `agent.py` in a terminal, warnings and errors are shown twice because both streams go to the screen.

`httpx` request logging is turned off in the agent and the sports server, because the request URLs would otherwise expose the Slack webhook URL and the YouTube API key.

## Error handling

- **Missing env vars** — The agent validates all required environment variables at startup and exits with a clear error message if any are missing.
- **Pool exhausted** — If the teaser pool runs out, an error is logged reminding you to regenerate. The agent still posts sports and calendar content.
- **API retries** — Both ESPN and YouTube API calls use exponential backoff with 3 retries for server errors (5xx). Client errors (4xx) fail immediately without retry.
- **Atomic writes** — Pool and memory files are written to a `.tmp` file first, then atomically renamed, to prevent corruption from partial writes.
- **YouTube scoring fallback** — If no videos meet the minimum duration threshold, the server returns no highlight rather than a short clip.
- **RSS feed retries** — The Italian headlines server uses exponential backoff with 3 retries for server errors (5xx), matching the same pattern as the sports server.
- **Wishlist fetch failures** — Amazon requests retry server and network errors 3 times with a longer backoff (15s, then 30s) because Amazon rate-limits with 503 responses. A CAPTCHA page, a client error (4xx) or a wishlist with no recognisable items posts a short failure message to `#wishlist`. A price watch failure never stops the other posts.
