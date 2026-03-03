"""
Morning Agent: Calendar + Sports + Brain Teaser
===============================================
Posts results to two Slack channels:
    #sports-highlights -> via webhook (sports highlights)
    #general           -> via Bot API (calendar summary + brain teaser with thread answer)

Run with:
    python agent/agent.py

Requirements in .env:
    ANTHROPIC_API_KEY=your_key_here
    GOOGLE_SHARED_CALENDAR_ID=your-shared-calendar@group.calendar.google.com
    SLACK_BOT_TOKEN=xoxb-your-token-here
    SLACK_CHANNEL_GENERAL=C08XXXXXXXXX
    SLACK_WEBHOOK_SPORTS=https://hooks.slack.com/services/...
"""

import asyncio
import json
import os
import random
from datetime import date
from typing import cast

import anthropic
from anthropic.types import MessageParam
import httpx
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SLACK_WEBHOOK_SPORTS = os.getenv("SLACK_WEBHOOK_SPORTS")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_GENERAL = os.getenv("SLACK_CHANNEL_GENERAL")

VENV_PYTHON = os.path.join(
    os.path.dirname(__file__),
    "..", "venv", "bin", "python"
)

SPORTS_SERVER_SCRIPT = os.path.join(
    os.path.dirname(__file__),
    "..", "servers", "sports_highlights_server.py"
)

CALENDAR_SERVER_SCRIPT = os.path.join(
    os.path.dirname(__file__),
    "..", "servers", "calendar_server.py"
)

MEMORY_FILE = os.path.join(
    os.path.dirname(__file__),
    "brain_teaser_memory.json"
)

TEASER_CATEGORIES = [
    "Riddle",
    "Logic puzzle",
    "Lateral thinking",
    "Maths puzzle",
    "Word puzzle",
    "Visual/spatial puzzle (described in text)"
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
        "paradox or counterintuitive outcome scenario",
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
    if not os.path.exists(MEMORY_FILE):
        return {
            "recent_categories": [],
            "difficulty_cycle_position": 0,
            "history": []
        }
    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_memory(memory: dict) -> None:
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memory, f, indent=2)
    print(f"[Memory] Saved brain teaser memory to {MEMORY_FILE}")


def pick_teaser_config(memory: dict) -> tuple[str, str, str]:
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

    print(
        f"[Memory] Today's brain teaser: category='{category}', "
        f"sub_type='{sub_type}', difficulty='{difficulty}'")
    print(f"[Memory] Recent categories (excluded): {recent}")
    print(f"[Memory] Recent sub-types for '{category}' (excluded): {recent_sub_types}")

    return category, sub_type, difficulty


def update_memory(memory: dict, category: str, sub_type: str) -> dict:
    recent = memory.get("recent_categories", [])
    recent.append(category)
    memory["recent_categories"] = recent[-3:]

    position = memory.get("difficulty_cycle_position", 0)
    memory["difficulty_cycle_position"] = (
        position + 1) % len(DIFFICULTY_CYCLE)

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
        "difficulty": DIFFICULTY_CYCLE[position % len(DIFFICULTY_CYCLE)]
    })
    memory["history"] = history

    return memory


def mcp_tools_to_anthropic(mcp_tools) -> list[dict]:
    """
    Converts MCP tool definitions to the shape Anthropic's API expects.
    inputSchema (MCP) -> input_schema (Anthropic)
    """
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.inputSchema
        }
        for tool in mcp_tools
    ]


async def post_to_webhook(webhook_url: str, message: str) -> bool:
    if not webhook_url:
        print("[Slack] Sports webhook URL not configured - skipping")
        return False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(webhook_url, json={"text": message})
            response.raise_for_status()
            print("[Slack] Sports highlights posted via webhook")
            return True
    except httpx.HTTPError as e:
        print(f"[Slack] Webhook post failed: {e}")
        return False


def post_with_thread_reply(
        channel: str,
        message: str,
        thread_reply: str) -> bool:
    """
    Post a message to a Slack channel using the Bot API,
    then reply in its thread with a follow-up message.
    """
    if not SLACK_BOT_TOKEN:
        print("[Slack] Bot token not configured - skipping")
        return False

    client = WebClient(token=SLACK_BOT_TOKEN)

    try:
        result = client.chat_postMessage(channel=channel, text=message)
        ts = result["ts"]
        print(f"[Slack] Posted brain teaser to #{channel} (ts: {ts})")

        client.chat_postMessage(
            channel=channel,
            text=thread_reply,
            thread_ts=ts
        )
        print("[Slack] Posted thread reply with answer")
        return True

    except SlackApiError as e:
        print(f"[Slack] Bot API error: {e.response['error']}")
        return False


def post_message(channel: str, message: str) -> bool:
    if not SLACK_BOT_TOKEN:
        print("[Slack] Bot token not configured - skipping")
        return False

    client = WebClient(token=SLACK_BOT_TOKEN)

    try:
        client.chat_postMessage(channel=channel, text=message)
        print(f"[Slack] Posted calendar summary to channel {channel}")
        return True
    except SlackApiError as e:
        print(f"[Slack] Bot API error: {e.response['error']}")
        return False


def parse_sections(final_text: str) -> dict[str, str]:
    """
    Split Claude's response into three named sections using markers:

        ##SPORTS##      -> #sports-highlights via webhook
        ##CALENDAR##    -> #general via Bot API (plain message)
        ##BRAINTEASER## -> #general via Bot API (with thread answer)
    """
    sections = {
        "sports": "",
        "calendar": "",
        "teaser_question": "",
        "teaser_answer": ""}

    print(f"[Parser] Parsing response ({len(final_text)} chars)")

    def find_marker(text: str, marker: str) -> list[int]:
        return [i for i in range(
            len(text)) if text[i:i + len(marker)] == marker]

    sports_pos = find_marker(final_text, "##SPORTS##")
    calendar_pos = find_marker(final_text, "##CALENDAR##")
    teaser_pos = find_marker(final_text, "##BRAINTEASER##")

    print(f"[Parser] Markers found - SPORTS: {len(sports_pos)}, "
          f"CALENDAR: {len(calendar_pos)}, BRAINTEASER: {len(teaser_pos)}")

    try:
        if sports_pos:
            start = sports_pos[0] + len("##SPORTS##")
            end = calendar_pos[0] if calendar_pos else teaser_pos[0] if teaser_pos else len(
                final_text)
            sections["sports"] = final_text[start:end].strip()

        if calendar_pos:
            start = calendar_pos[0] + len("##CALENDAR##")
            end = teaser_pos[0] if teaser_pos else len(final_text)
            sections["calendar"] = final_text[start:end].strip()

        if teaser_pos:
            start = teaser_pos[0] + len("##BRAINTEASER##")
            raw = final_text[start:].strip()

            if "##BRAINTEASER##" in raw:
                raw = raw.split("##BRAINTEASER##")[0].strip()

            if "|" in raw:
                parts = raw.split("|", 1)
                sections["teaser_question"] = parts[0].strip()
                sections["teaser_answer"] = f"Answer: {parts[1].strip()}"
            else:
                sections["teaser_question"] = raw
                sections["teaser_answer"] = "(No answer provided)"

    except Exception as e:
        print(f"[Parser] Error: {e} - sections may be incomplete")

    return sections


async def run_agent(
    calendar_session: ClientSession,
    sports_session: ClientSession,
    anthropic_client: anthropic.Anthropic,
    teaser_category: str,
    teaser_sub_type: str,
    teaser_difficulty: str
) -> str:
    """
    The agent loop - routes tool calls across two MCP servers and
    returns Claude's final response text for Slack posting.

    Parameters:
        calendar_session:   Active MCP session for the Google Calendar server
        sports_session:     Active MCP session for the sports highlights server
        anthropic_client:   Anthropic API client
        teaser_category:    Brain teaser category chosen from memory
        teaser_sub_type:    Brain teaser sub-type for variety within the category
        teaser_difficulty:  Brain teaser difficulty from the cycle
    """

    print("\n--- Discovering tools from both MCP servers ---")

    calendar_tools_response = await calendar_session.list_tools()
    sports_tools_response = await sports_session.list_tools()

    tool_routing: dict[str, ClientSession] = {}
    for tool in calendar_tools_response.tools:
        tool_routing[tool.name] = calendar_session
    for tool in sports_tools_response.tools:
        tool_routing[tool.name] = sports_session

    all_mcp_tools = calendar_tools_response.tools + sports_tools_response.tools
    all_tools = mcp_tools_to_anthropic(all_mcp_tools)

    print(
        f"Calendar server tools: {[t.name for t in calendar_tools_response.tools]}")
    print(
        f"Sports server tools:   {[t.name for t in sports_tools_response.tools]}")
    print(f"Total tools available to Claude: {len(all_tools)}")

    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                "Please do my morning checks and format your response using "
                "the exact section markers below. Each section must start and "
                "end with its marker.\n\n"

                "##SPORTS##\n"
                "Check if any New England sports teams (Patriots, Bruins, "
                "Celtics, Red Sox, Revolution) played yesterday. For each "
                "team that played, include the YouTube highlight link. "
                "Avoid including any scores or spoilers in the text - just the highlights. "
                "If no teams played, say so.\n"
                "##SPORTS##\n\n"

                "##CALENDAR##\n"
                "Fetch today's calendar events and give me a clear summary "
                "of my day. List each event with its time and title. "
                "If I have no events today, say so.\n"
                "##CALENDAR##\n\n"

                f"##BRAINTEASER##\n"
                f"Generate a {teaser_difficulty} difficulty {teaser_category} "
                f"of the sub-type: {teaser_sub_type}. "
                f"Format it exactly like this - "
                f"question text | answer text "
                f"(a single pipe character separating question from answer, "
                f"everything on one line, no labels).\n"
                "##BRAINTEASER##\n\n"

                "Important: keep all markers exactly as shown. "
                "They are used to route each section automatically."
            )
        }
    ]

    print("\n--- Starting agent loop ---")

    while True:

        print(f"\n[Loop] Sending {len(messages)} message(s) to Claude...")
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=2048,
            tools=cast(list, all_tools),
            messages=cast(list[MessageParam], messages)
        )

        print(f"[Loop] Claude stop reason: '{response.stop_reason}'")

        if response.stop_reason == "end_turn":
            final_text = next(
                block.text for block in response.content
                if block.type == "text"
            )
            print(f"\n--- Final response from Claude ---\n{final_text}")
            return final_text

        if response.stop_reason == "tool_use":

            messages.append({
                "role": "assistant",
                "content": [block.model_dump() for block in response.content]
            })

            tool_results: list[dict] = []

            for block in response.content:
                if block.type != "tool_use":
                    continue

                print(f"\n[Tool call] '{block.name}'")
                print(
                    f"[Tool call] Arguments: {
                        json.dumps(
                            block.input,
                            indent=2)}")

                session = tool_routing.get(block.name)
                if session is None:
                    result_text = f"Error: no server found for tool '{
                        block.name}'"
                    print(f"[Tool error] {result_text}")
                else:
                    tool_response = await session.call_tool(block.name, block.input)
                    first = tool_response.content[0] if tool_response.content else None
                    result_text = (
                        first.text
                        if first is not None and first.type == "text"
                        else "No result"
                    )
                    print(
                        f"[Tool result] {result_text[:200]}{'...' if len(result_text) > 200 else ''}")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text
                })

            messages.append({
                "role": "user",
                "content": tool_results
            })


async def main():
    print("=== Morning Agent ===")

    memory = load_memory()
    teaser_category, teaser_sub_type, teaser_difficulty = pick_teaser_config(memory)

    calendar_server_params = StdioServerParameters(
        command=VENV_PYTHON,
        args=[CALENDAR_SERVER_SCRIPT],
        env={**os.environ}
    )

    sports_server_params = StdioServerParameters(
        command=VENV_PYTHON,
        args=[SPORTS_SERVER_SCRIPT],
        env={**os.environ}
    )

    anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    print("Connecting to calendar server...")
    async with stdio_client(calendar_server_params) as (cal_read, cal_write):
        async with ClientSession(cal_read, cal_write) as calendar_session:
            await calendar_session.initialize()
            print("Connected to calendar server.")

            print("Connecting to sports highlights server...")
            async with stdio_client(sports_server_params) as (sports_read, sports_write):
                async with ClientSession(sports_read, sports_write) as sports_session:
                    await sports_session.initialize()
                    print("Connected to sports highlights server.")

                    final_text = await run_agent(
                        calendar_session,
                        sports_session,
                        anthropic_client,
                        teaser_category,
                        teaser_sub_type,
                        teaser_difficulty
                    )

    print("\n--- Posting to Slack ---")
    sections = parse_sections(final_text)

    if sections["sports"] and SLACK_WEBHOOK_SPORTS:
        await post_to_webhook(SLACK_WEBHOOK_SPORTS, sections["sports"])
    else:
        print("[Slack] No sports content to post")

    if sections["calendar"] and SLACK_CHANNEL_GENERAL:
        post_message(SLACK_CHANNEL_GENERAL, sections["calendar"])
    else:
        print("[Slack] No calendar content to post")

    if sections["teaser_question"] and SLACK_CHANNEL_GENERAL:
        post_with_thread_reply(
            SLACK_CHANNEL_GENERAL,
            sections["teaser_question"],
            sections["teaser_answer"]
        )
    else:
        print("[Slack] No brain teaser content to post")

    memory = update_memory(memory, teaser_category, teaser_sub_type)
    save_memory(memory)

    print("\n=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
