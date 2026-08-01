"""
Morning Agent: Calendar + Sports + Brain Teaser + Italian Headlines
====================================================================
Posts results to Slack channels:
    #sports-highlights  -> via webhook (sports highlights)
    #calendar           -> via Bot API (calendar summary)
    #brain-teaser       -> via Bot API (brain teaser with thread answer)
    #italian-football   -> via Bot API (Italian headlines with English translations in thread)

Run with:
    python agent/agent.py

Requirements in .env:
    ANTHROPIC_API_KEY=your_key_here
    GOOGLE_SHARED_CALENDAR_ID=your-shared-calendar@group.calendar.google.com
    SLACK_BOT_TOKEN=xoxb-your-token-here
    SLACK_CHANNEL_BRAIN_TEASER=C08XXXXXXXXX
    SLACK_CHANNEL_CALENDAR=C08XXXXXXXXX
    SLACK_CHANNEL_ITALIAN=C0BMACPHL3X
    SLACK_WEBHOOK_SPORTS=https://hooks.slack.com/services/...
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import cast

import anthropic
import httpx
from anthropic.types import MessageParam
from brain_teaser import (
    get_next_teaser,
    load_memory,
    load_pool,
    pool_needs_regeneration,
    record_teaser_in_history,
    save_memory,
    save_pool,
)
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("morning_agent")

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration — all secrets and tunables come from the environment
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SLACK_WEBHOOK_SPORTS = os.getenv("SLACK_WEBHOOK_SPORTS")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_BRAIN_TEASER = os.getenv("SLACK_CHANNEL_BRAIN_TEASER")
SLACK_CHANNEL_CALENDAR = os.getenv("SLACK_CHANNEL_CALENDAR")
SLACK_CHANNEL_ITALIAN = os.getenv("SLACK_CHANNEL_ITALIAN")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")

REQUIRED_ENV_VARS = [
    "ANTHROPIC_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_CHANNEL_BRAIN_TEASER",
    "SLACK_CHANNEL_CALENDAR",
    "SLACK_CHANNEL_ITALIAN",
]

# Use the current Python interpreter so MCP servers inherit the same venv
PYTHON_EXECUTABLE = sys.executable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPORTS_SERVER_SCRIPT = str(
    PROJECT_ROOT /
    "servers" /
    "sports_highlights_server.py")
CALENDAR_SERVER_SCRIPT = str(PROJECT_ROOT / "servers" / "calendar_server.py")
ITALIAN_SERVER_SCRIPT = str(
    PROJECT_ROOT /
    "servers" /
    "italian_headlines_server.py")

# Reusable Slack client — created once, reused for all Bot API calls
_slack_client: WebClient | None = None


def _get_slack_client() -> WebClient:
    """Return a lazily-initialised, reusable Slack WebClient."""
    global _slack_client
    if _slack_client is None:
        _slack_client = WebClient(token=SLACK_BOT_TOKEN)
    return _slack_client


def mcp_tools_to_anthropic(mcp_tools) -> list[dict]:
    """
    Converts MCP tool definitions to the shape Anthropic's API expects.
    """
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        for tool in mcp_tools
    ]


async def post_to_webhook(webhook_url: str, message: str) -> bool:
    """Post a message to Slack via an incoming webhook URL."""
    if not webhook_url:
        logger.warning("Sports webhook URL not configured — skipping")
        return False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(webhook_url, json={"text": message})
            response.raise_for_status()
            logger.info("Sports highlights posted via webhook")
            return True
    except httpx.HTTPError as e:
        logger.error("Webhook post failed: %s", e)
        return False


def post_with_thread_reply(
    channel: str,
    message: str,
    thread_reply: str,
) -> bool:
    """Post a message to Slack, then reply in its thread with a follow-up."""
    if not SLACK_BOT_TOKEN:
        logger.warning("Bot token not configured — skipping")
        return False

    client = _get_slack_client()

    try:
        result = client.chat_postMessage(channel=channel, text=message)
        ts = result["ts"]
        logger.info("Posted brain teaser to #%s (ts: %s)", channel, ts)

        client.chat_postMessage(
            channel=channel,
            text=thread_reply,
            thread_ts=ts,
        )
        logger.info("Posted thread reply with answer")
        return True

    except SlackApiError as e:
        logger.error("Bot API error: %s", e.response["error"])
        return False


def post_message(channel: str, message: str) -> bool:
    """Post a simple message to a Slack channel using the Bot API."""
    if not SLACK_BOT_TOKEN:
        logger.warning("Bot token not configured — skipping")
        return False

    client = _get_slack_client()

    try:
        client.chat_postMessage(channel=channel, text=message)
        logger.info("Posted calendar summary to channel %s", channel)
        return True
    except SlackApiError as e:
        logger.error("Bot API error: %s", e.response["error"])
        return False


def parse_sections(final_text: str) -> dict[str, str]:
    """
    Split Claude's response into named sections using markers:

        ##SPORTS##      -> #sports-highlights via webhook
        ##CALENDAR##    -> #general via Bot API (plain message)
    """
    sections: dict[str, str] = {
        "sports": "",
        "calendar": "",
        "italian": "",
    }

    logger.info("Parsing response (%d chars)", len(final_text))

    try:
        if "##SPORTS##" in final_text:
            after_sports = final_text.split("##SPORTS##", 1)[1]
            end = len(after_sports)
            for marker in ["##CALENDAR##", "##ITALIAN##"]:
                pos = after_sports.find(marker)
                if pos != -1 and pos < end:
                    end = pos
            sections["sports"] = after_sports[:end].strip()

        if "##CALENDAR##" in final_text:
            after_calendar = final_text.split("##CALENDAR##", 1)[1]
            end = len(after_calendar)
            for marker in ["##ITALIAN##"]:
                pos = after_calendar.find(marker)
                if pos != -1 and pos < end:
                    end = pos
            sections["calendar"] = after_calendar[:end].strip()

        if "##ITALIAN##" in final_text:
            after_italian = final_text.split("##ITALIAN##", 1)[1]
            sections["italian"] = after_italian.strip()

    except Exception as e:
        logger.error("Parser error: %s — sections may be incomplete", e)

    return sections


def split_italian_section(section_text: str) -> tuple[str, str]:
    """
    Split the Italian headlines section into Italian text (for main message)
    and English translations (for thread reply).

    Lines containing 'EN:' are extracted as English translations.
    Everything else forms the Italian main message.
    """
    lines = section_text.split("\n")
    italian_lines = []
    english_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("EN:"):
            english_lines.append(stripped)
        else:
            italian_lines.append(line)

    italian_text = "\n".join(italian_lines).strip()
    english_text = "\n".join(english_lines).strip()

    if not english_text:
        english_text = "(No translations available)"

    return italian_text, english_text


async def run_agent(
    calendar_session: ClientSession,
    sports_session: ClientSession,
    italian_session: ClientSession,
    anthropic_client: anthropic.Anthropic,
) -> str:
    """
    The agent loop — routes tool calls across three MCP servers and
    returns Claude's final response text for Slack posting.

    Parameters:
        calendar_session:   Active MCP session for the Google Calendar server
        sports_session:     Active MCP session for the sports highlights server
        italian_session:    Active MCP session for the Italian headlines server
        anthropic_client:   Anthropic API client
    """

    logger.info("--- Discovering tools from all MCP servers ---")

    calendar_tools_response = await calendar_session.list_tools()
    sports_tools_response = await sports_session.list_tools()
    italian_tools_response = await italian_session.list_tools()

    tool_routing: dict[str, ClientSession] = {}
    for tool in calendar_tools_response.tools:
        tool_routing[tool.name] = calendar_session
    for tool in sports_tools_response.tools:
        tool_routing[tool.name] = sports_session
    for tool in italian_tools_response.tools:
        tool_routing[tool.name] = italian_session

    all_mcp_tools = calendar_tools_response.tools + \
        sports_tools_response.tools + italian_tools_response.tools
    all_tools = mcp_tools_to_anthropic(all_mcp_tools)

    logger.info(
        "Calendar server tools: %s",
        [t.name for t in calendar_tools_response.tools],
    )
    logger.info(
        "Sports server tools:   %s",
        [t.name for t in sports_tools_response.tools],
    )
    logger.info(
        "Italian server tools: %s",
        [t.name for t in italian_tools_response.tools],
    )
    logger.info("Total tools available to Claude: %d", len(all_tools))

    messages: list[dict] = [
        {
            "role": "user", "content": (
                "Please do my morning checks and format your response using "
                "the exact section markers below. Each section must start and "
                "end with its marker.\n\n"
                "##SPORTS##\n"
                "Check if any tracked teams played yesterday: "
                "New England sports teams (Patriots, Bruins, "
                "Celtics, Red Sox, Revolution) and Palermo F.C. "
                "For each team that played, include the YouTube highlight link. "
                "Avoid including any scores or spoilers in the text - just the highlights. "
                "If no teams played, say so.\n"
                "##SPORTS##\n\n"
                "##CALENDAR##\n"
                "Fetch today's calendar events and give me a clear summary "
                "of my day. List each event with its time and title. "
                "If I have no events today, say so.\n"
                "##CALENDAR##\n\n"
                "##ITALIAN##\n"
                "Use the italian_get_headlines tool to fetch recent Italian football headlines. "
                "For each headline: include the original Italian title, the source name, "
                "and the link. Then provide an English translation of each headline on a "
                "separate line prefixed with 'EN: '. Format as a numbered list.\n"
                "Example format:\n"
                "1. *Italian Title Here* — Source Name\n"
                "   EN: English Translation Here\n"
                "   <link>\n"
                "##ITALIAN##\n\n"
                "Important: keep all markers exactly as shown. "
                "They are used to route each section automatically."), }]

    logger.info("--- Starting agent loop ---")

    while True:
        logger.info("Sending %d message(s) to Claude…", len(messages))
        response = anthropic_client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2048,
            tools=cast(list, all_tools),
            messages=cast(list[MessageParam], messages),
        )

        logger.info("Claude stop reason: '%s'", response.stop_reason)

        if response.stop_reason == "end_turn":
            final_text = next(
                block.text for block in response.content if block.type == "text")
            logger.info("--- Final response from Claude ---\n%s", final_text)
            return final_text

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": [
                block.model_dump() for block in response.content], })

            tool_results: list[dict] = []

            for block in response.content:
                if block.type != "tool_use":
                    continue

                logger.info("Tool call: '%s'", block.name)
                logger.debug(
                    "Tool arguments: %s", json.dumps(
                        block.input, indent=2))

                session = tool_routing.get(block.name)
                if session is None:
                    result_text = f"Error: no server found for tool '{
                        block.name}'"
                    logger.error(result_text)
                else:
                    tool_response = await session.call_tool(block.name, block.input)
                    first = tool_response.content[0] if tool_response.content else None
                    result_text = first.text if first is not None and first.type == "text" else "No result"
                    logger.info("Tool result: %s", result_text[:200])

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    }
                )

            messages.append(
                {
                    "role": "user",
                    "content": tool_results,
                }
            )


async def main():
    logger.info("=== Morning Agent ===")

    # Validate required environment variables early
    missing = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
    if missing:
        logger.error(
            "Missing required environment variables: %s. Check your .env file.",
            ", ".join(missing),
        )
        return

    # --- Brain teaser pool ---
    pool = load_pool()
    teaser = get_next_teaser(pool)

    if teaser is None:
        logger.error(
            "No teasers available in the pool. Run `python agent/generate_teaser_pool.py` to generate a new batch."
        )
    elif pool_needs_regeneration(pool):
        logger.warning(
            "Teaser pool is running low (%d remaining). Run `python agent/generate_teaser_pool.py` soon to replenish.",
            len(pool["teasers"]) - pool["next_index"],
        )

    calendar_server_params = StdioServerParameters(
        command=PYTHON_EXECUTABLE,
        args=[CALENDAR_SERVER_SCRIPT],
        env={**os.environ},
    )

    sports_server_params = StdioServerParameters(
        command=PYTHON_EXECUTABLE,
        args=[SPORTS_SERVER_SCRIPT],
        env={**os.environ},
    )

    anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    italian_server_params = StdioServerParameters(
        command=PYTHON_EXECUTABLE,
        args=[ITALIAN_SERVER_SCRIPT],
        env={**os.environ},
    )

    logger.info("Connecting to calendar server…")
    async with (
        stdio_client(calendar_server_params) as (cal_read, cal_write),
        ClientSession(cal_read, cal_write) as calendar_session,
    ):
        await calendar_session.initialize()
        logger.info("Connected to calendar server.")

        logger.info("Connecting to sports highlights server…")
        async with (
            stdio_client(sports_server_params) as (sports_read, sports_write),
            ClientSession(sports_read, sports_write) as sports_session,
        ):
            await sports_session.initialize()
            logger.info("Connected to sports highlights server.")

            logger.info("Connecting to Italian headlines server…")
            async with (
                stdio_client(italian_server_params) as (italian_read, italian_write),
                ClientSession(italian_read, italian_write) as italian_session,
            ):
                await italian_session.initialize()
                logger.info("Connected to Italian headlines server.")

                final_text = await run_agent(
                    calendar_session,
                    sports_session,
                    italian_session,
                    anthropic_client,
                )

    logger.info("--- Posting to Slack ---")
    sections = parse_sections(final_text)

    if sections["sports"] and SLACK_WEBHOOK_SPORTS:
        await post_to_webhook(SLACK_WEBHOOK_SPORTS, sections["sports"])
    else:
        logger.info("No sports content to post")

    if sections["calendar"] and SLACK_CHANNEL_CALENDAR:
        post_message(SLACK_CHANNEL_CALENDAR, sections["calendar"])
    else:
        logger.info("No calendar content to post")

    # Post Italian headlines — main message is the Italian text,
    # thread reply contains the English translations extracted from the section
    if sections["italian"] and SLACK_CHANNEL_ITALIAN:
        italian_text, english_text = split_italian_section(sections["italian"])
        post_with_thread_reply(
            SLACK_CHANNEL_ITALIAN,
            italian_text,
            english_text,
        )
    else:
        logger.info("No Italian content to post")

    # Post brain teaser from the pool (no Claude generation needed)
    if teaser and SLACK_CHANNEL_BRAIN_TEASER:
        post_with_thread_reply(
            SLACK_CHANNEL_BRAIN_TEASER,
            teaser["question"],
            f"Answer: {teaser['answer']}",
        )
        # Record in memory for cross-batch dedup
        memory = load_memory()
        memory = record_teaser_in_history(memory, teaser)
        save_memory(memory)
    else:
        logger.info("No brain teaser to post")

    # Save pool state (marks this teaser as consumed)
    if pool.get("teasers"):
        save_pool(pool)

    logger.info("=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
