"""
Google Calendar MCP Server
==========================
I'm fetching the following Google calendars:
    - Primary calendar.
    - A shared calendar (configured via GOOGLE_SHARED_CALENDAR_ID).

Requirements in .env:
    GOOGLE_CREDENTIALS_PATH=/absolute/path/to/agent/credentials.json
    GOOGLE_SHARED_CALENDAR_ID=your-shared-calendar@group.calendar.google.com

Install:
    pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("calendar_server")

load_dotenv()

GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH")
GOOGLE_SHARED_CALENDAR_ID = os.getenv("GOOGLE_SHARED_CALENDAR_ID")

TOKEN_PATH = str(
    Path(GOOGLE_CREDENTIALS_PATH).parent / "token.json"
) if GOOGLE_CREDENTIALS_PATH else "token.json"

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

LOCAL_TIMEZONE = "Europe/London"


def get_calendar_service():
    """
    Authenticate with Google Calendar API and return a service object.
    """
    creds = None

    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        logger.info("Loaded existing token from token.json")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Token expired - refreshing...")
            creds.refresh(Request())
        else:
            logger.info("No valid token found - starting OAuth flow...")
            if not GOOGLE_CREDENTIALS_PATH:
                raise ValueError(
                    "GOOGLE_CREDENTIALS_PATH not set in .env. "
                    "Download credentials.json from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                GOOGLE_CREDENTIALS_PATH, SCOPES
            )
            # Opens browser for user to log in and grant access
            creds = flow.run_local_server(port=0)

        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
        logger.info(f"Token saved to {TOKEN_PATH}")

    return build("calendar", "v3", credentials=creds)


class CalendarDataManager:
    """
    Fetches and merges events from Google Calendar.
    """

    def get_todays_events(self) -> list[dict]:
        """
        Returns:
            List of event dicts sorted by start time, each containing:
                title, start_time, end_time, calendar, location, description
        """
        tz = ZoneInfo(LOCAL_TIMEZONE)
        now = datetime.now(tz)

        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)

        time_min = start_of_day.isoformat()
        time_max = end_of_day.isoformat()

        logger.info(f"Fetching events for {now.date()} ({LOCAL_TIMEZONE})")

        try:
            service = get_calendar_service()
        except Exception as e:
            logger.error(f"Failed to authenticate with Google Calendar: {e}")
            raise

        calendars_to_fetch = [
            ("primary", "Personal"),
        ]

        if GOOGLE_SHARED_CALENDAR_ID:
            calendars_to_fetch.append(
                (GOOGLE_SHARED_CALENDAR_ID, "Shared")
            )
        else:
            logger.warning(
                "GOOGLE_SHARED_CALENDAR_ID not set - fetching primary only")

        all_events = []

        for calendar_id, calendar_label in calendars_to_fetch:
            try:
                result = service.events().list(
                    calendarId=calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=50
                ).execute()

                events = result.get("items", [])
                logger.info(
                    f"Fetched {
                        len(events)} events from {calendar_label} calendar")

                for event in events:
                    parsed = self._parse_event(event, calendar_label)
                    if parsed:  # None means it was an all-day event
                        all_events.append(parsed)

            except HttpError as e:
                logger.error(
                    f"Google Calendar API error for {calendar_label}: {e}")
                continue

        all_events.sort(key=lambda e: e["start_time"])

        logger.info(f"Total timed events today: {len(all_events)}")
        return all_events

    def _parse_event(self, event: dict, calendar_label: str) -> dict | None:
        start = event.get("start", {})
        end = event.get("end", {})

        if "dateTime" not in start:
            return None

        start_dt = datetime.fromisoformat(start["dateTime"])
        end_dt = datetime.fromisoformat(end["dateTime"])

        tz = ZoneInfo(LOCAL_TIMEZONE)
        start_local = start_dt.astimezone(tz)
        end_local = end_dt.astimezone(tz)

        return {
            "title": event.get("summary", "No title"),
            "start_time": start_local.strftime("%H:%M"),
            "end_time": end_local.strftime("%H:%M"),
            "start_iso": start_local.isoformat(),
            "calendar": calendar_label,
            "location": event.get("location", ""),
            "description": event.get("description", "")[:200]
        }

    def get_stats(self) -> dict:
        calendars = ["Primary (personal)"]
        if GOOGLE_SHARED_CALENDAR_ID:
            calendars.append(f"Shared ({GOOGLE_SHARED_CALENDAR_ID})")

        return {
            "calendars": calendars,
            "timezone": LOCAL_TIMEZONE,
            "scope": "Today's timed events only (all-day events excluded)",
            "data_source": "Google Calendar API v3"
        }


server = Server("calendar_server")
data_manager = CalendarDataManager()


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="calendar_get_todays_events",
            description=(
                "Get today's scheduled events from Google Calendar. "
                "Returns a merged, chronological list of timed events "
                "from the primary and shared calendars. "
                "All-day events are excluded. "
                "Use this to give a summary of the day ahead."
            ),
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        types.Tool(
            name="calendar_get_stats",
            description=(
                "Get an overview of which calendars this server connects to."
            ),
            inputSchema={
                "type": "object",
                "properties": {}
            }
        )
    ]


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent]:

    try:
        match name:
            case "calendar_get_todays_events":
                loop = asyncio.get_event_loop()
                events = await loop.run_in_executor(
                    None, data_manager.get_todays_events
                )

                if not events:
                    return [types.TextContent(
                        type="text",
                        text="No timed events scheduled for today."
                    )]

                return [types.TextContent(
                    type="text",
                    text=json.dumps(events, indent=2, ensure_ascii=False)
                )]

            case "calendar_get_stats":
                return [types.TextContent(
                    type="text",
                    text=json.dumps(data_manager.get_stats(), indent=2)
                )]

            case _:
                raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        logger.error(f"Error executing tool '{name}': {e}", exc_info=True)
        return [types.TextContent(
            type="text",
            text=f"Error executing tool: {str(e)}"
        )]


async def main():
    try:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            logger.info("Google Calendar MCP Server starting...")
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="calendar_server",
                    server_version="1.0.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
    except Exception as e:
        logger.error(f"Server error: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    asyncio.run(main())
