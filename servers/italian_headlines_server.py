"""
Italian Football Headlines MCP Server
======================================
Fetches recent Italian-language football (calcio) headlines from Google News RSS.
Designed for Italian language learners — headlines come from Italian publications
like Gazzetta dello Sport, Sky Sport, Corriere dello Sport, Tuttosport, etc.

The headlines are in Italian, so they can be posted to Slack for language immersion,
with English translations posted in a thread reply.

Requirements:
    pip install httpx mcp python-dotenv feedparser
"""

import asyncio
import json
import logging
import os

import feedparser
import httpx
from dotenv import load_dotenv
from mcp.server import MCPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("italian_headlines_server")

load_dotenv()

# Google News RSS URL for Italian football headlines
RSS_URL = "https://news.google.com/rss/search?q=calcio+serie+A&hl=it&gl=IT&ceid=IT:it"

NUM_HEADLINES = int(os.getenv("ITALIAN_NUM_HEADLINES", "3"))

MAX_RETRIES = 3
RETRY_BACKOFF_FACTOR = 1  # seconds, doubles each retry


async def fetch_rss_feed() -> str:
    """
    Fetch the Google News RSS feed for Italian calcio headlines.
    Uses exponential backoff retries on server errors.

    Returns:
        Raw RSS XML content as a string.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(RSS_URL)
                response.raise_for_status()
                return response.text
        except httpx.HTTPStatusError as e:
            # Don't retry client errors (4xx)
            if e.response.status_code < 500:
                logger.error("RSS feed client error: %s", e)
                return ""
            logger.warning("RSS feed retry %d/%d: %s", attempt, MAX_RETRIES, e)
        except httpx.HTTPError as e:
            logger.warning("RSS feed retry %d/%d: %s", attempt, MAX_RETRIES, e)
        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_BACKOFF_FACTOR * 2 ** (attempt - 1))

    logger.error("RSS feed failed after %d retries", MAX_RETRIES)
    return ""


def parse_headlines(rss_content: str, count: int = NUM_HEADLINES) -> list[dict]:
    """
    Parse RSS content and extract the top headlines.

    Args:
        rss_content: Raw RSS XML string from Google News.
        count: Number of headlines to return (default: 3).

    Returns:
        List of dicts with title, source, url, and published fields.
    """
    if not rss_content:
        return []

    feed = feedparser.parse(rss_content)

    if not feed.entries:
        logger.warning("No entries found in RSS feed")
        return []

    headlines = []
    for entry in feed.entries[:count]:
        headline = {
            "title": entry.get("title", ""),
            "source": entry.get("source", {}).get("title", "") if isinstance(entry.get("source"), dict) else str(entry.get("source", "")),
            "url": entry.get("link", ""),
            "published": entry.get("published", ""),
        }
        headlines.append(headline)
        logger.info("Headline: %s — %s", headline["source"], headline["title"])

    return headlines


class ItalianHeadlinesManager:
    """Manages fetching and processing Italian football headlines."""

    async def get_headlines(self, count: int = NUM_HEADLINES) -> list[dict]:
        """
        Fetch and parse Italian football headlines from Google News RSS.

        Args:
            count: Number of headlines to return (default: 3).

        Returns:
            List of headline dicts with title, source, url, published.
        """
        rss_content = await fetch_rss_feed()
        headlines = parse_headlines(rss_content, count)

        if not headlines:
            logger.warning("No Italian headlines available")
            return []

        return headlines

    def get_stats(self) -> dict:
        return {
            "source": "Google News RSS",
            "feed_url": RSS_URL,
            "language": "Italian",
            "topic": "Calcio (Serie A football)",
            "default_count": NUM_HEADLINES,
            "note": "Headlines sourced from Italian sports publications",
        }


server = MCPServer("italian_headlines_server")
manager = ItalianHeadlinesManager()


@server.tool(
    name="italian_get_headlines",
    description=(
        "Fetch recent Italian-language football (calcio) headlines from Italian "
        "news sources. Returns the top headlines about Serie A from publications "
        "like Gazzetta dello Sport, Sky Sport, Corriere dello Sport, and Tuttosport. "
        "Each headline includes the Italian title, source name, URL, and publication date. "
        "Use this to get Italian football news for language learners."
    ),
)
async def handle_get_headlines(count: int | None = None) -> str:
    """Get Italian calcio headlines from Google News RSS."""
    try:
        headlines = await manager.get_headlines(count=count or NUM_HEADLINES)

        if not headlines:
            return "No Italian football headlines available right now."

        return json.dumps(headlines, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error("Error in italian_get_headlines: %s", e, exc_info=True)
        return f"Error executing tool: {e}"


@server.tool(
    name="italian_get_stats",
    description="Get info about the Italian headlines data source.",
)
async def handle_get_stats() -> str:
    """Get stats about the Italian headlines data source."""
    try:
        return json.dumps(manager.get_stats(), indent=2)
    except Exception as e:
        logger.error("Error in italian_get_stats: %s", e, exc_info=True)
        return f"Error executing tool: {e}"


def main():
    logger.info("Italian Headlines MCP Server starting...")
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
