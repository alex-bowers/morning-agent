"""
Sports Highlights MCP Server
============================
I like New England sports teams.
This get game highlights by checking if each team has played and then it searches Youtube for a video.

Requirements in .env:
    YOUTUBE_API_KEY=your_key_here

Install:
    pip install httpx mcp python-dotenv
"""

import asyncio
import json
import logging
import os
import re
from datetime import date, timedelta

import httpx
from dotenv import load_dotenv
from mcp.server import MCPServer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("sports_highlights_server")

load_dotenv()

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
MIN_DURATION_SECONDS = int(os.getenv("MIN_DURATION_SECONDS", str(5 * 60)))


MAX_RETRIES = 3
RETRY_BACKOFF_FACTOR = 1  # seconds, doubles each retry


def _parse_iso8601_duration(duration: str) -> int:
    """
    Parse an ISO 8601 duration string (e.g. 'PT6M30S') into total seconds.
    Returns 0 if parsing fails.
    """
    match = re.match(
        r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?',
        duration
    )
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


# Unofficial API
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"

TRACKED_TEAMS = {
    "New England Patriots": {
        "sport": "football",
        "league": "nfl",
        "espn_name": "New England Patriots",
        "youtube_search": "New England Patriots highlights"
    },
    "Boston Celtics": {
        "sport": "basketball",
        "league": "nba",
        "espn_name": "Boston Celtics",
        "youtube_search": "Boston Celtics highlights"
    },
    "Boston Bruins": {
        "sport": "hockey",
        "league": "nhl",
        "espn_name": "Boston Bruins",
        "youtube_search": "Boston Bruins highlights"
    },
    "Boston Red Sox": {
        "sport": "baseball",
        "league": "mlb",
        "espn_name": "Boston Red Sox",
        "youtube_search": "Boston Red Sox highlights"
    },
    "New England Revolution": {
        "sport": "soccer",
        "league": "usa.1",
        "espn_name": "New England Revolution",
        "youtube_search": "New England Revolution highlights"
    },
    "Palermo F.C.": {
        "sport": "soccer",
        "league": "ita.2",
        "espn_name": "Palermo",
        "youtube_search": "Palermo highlights"
    }
}


async def fetch_espn_scoreboard(
        sport: str,
        league: str,
        game_date: str) -> dict:
    """
    Args:
        sport:     e.g. 'football', 'basketball', 'hockey', 'baseball', 'soccer'
        league:    e.g. 'nfl', 'nba', 'nhl', 'mlb', 'usa.1'
        game_date: date string in YYYYMMDD format
    """
    url = ESPN_SCOREBOARD_URL.format(sport=sport, league=league)
    params = {"dates": game_date}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            # Don't retry client errors (4xx)
            if e.response.status_code < 500:
                logger.error(f"ESPN API client error for {sport}/{league}: {e}")
                return {}
            logger.warning(
                f"ESPN API retry {attempt}/{MAX_RETRIES} for {sport}/{league}: {e}"
            )
        except httpx.HTTPError as e:
            logger.warning(
                f"ESPN API retry {attempt}/{MAX_RETRIES} for {sport}/{league}: {e}"
            )
        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_BACKOFF_FACTOR * 2 ** (attempt - 1))

    logger.error(f"ESPN API failed after {MAX_RETRIES} retries for {sport}/{league}")
    return {}


def extract_game_from_espn(espn_data: dict, team_name: str) -> dict | None:
    """
    ESPN's scoreboard returns a list of 'events'.
    Each event has two 'competitors' (home and away).
    We look for our team in either slot.

    Returns a dict with game details if found, None otherwise.
    """
    events = espn_data.get("events", [])

    for event in events:
        competitions = event.get("competitions", [])
        for competition in competitions:
            competitors = competition.get("competitors", [])

            home_team = next(
                (c for c in competitors if c.get("homeAway") == "home"), None)
            away_team = next(
                (c for c in competitors if c.get("homeAway") == "away"), None)

            if not home_team or not away_team:
                continue

            home_name = home_team.get("team", {}).get("displayName", "")
            away_name = away_team.get("team", {}).get("displayName", "")

            if team_name.lower() not in [home_name.lower(), away_name.lower()]:
                continue

            home_score = home_team.get("score", "?")
            away_score = away_team.get("score", "?")

            our_team = home_team if team_name.lower() == home_name.lower() else away_team
            opponent = away_team if team_name.lower() == home_name.lower() else home_team
            opponent_name = opponent.get(
                "team", {}).get(
                "displayName", "Unknown")

            our_score = int(our_team.get("score", 0) or 0)
            their_score = int(opponent.get("score", 0) or 0)

            if our_score > their_score:
                result = "Win"
            elif our_score < their_score:
                result = "Loss"
            else:
                result = "Draw"

            score = f"{away_score}-{home_score}"

            return {
                "team": team_name,
                "opponent": opponent_name,
                "score": score,
                "result": result,
                "game_date": event.get("date", "")[:10],  # trim to YYYY-MM-DD
                "game_name": event.get("name", f"{team_name} vs {opponent_name}")
            }

    return None


def _score_youtube_result(item: dict) -> int:
    """
    Score a YouTube search result to prefer official and high-quality sources.
    As a higher score is a better result.
    We check both the channel name and video title for signals.
    These indicate an official or reputable highlight video.

    Scoring:
        +3  Official league channel (NBA, NFL, NHL, MLB, MLS)
        +2  Team's own channel
        +1  Title contains 'full' or 'extended' (suggests complete highlights)
        -1  Title contains 'reaction' or 'fan' (suggests unofficial content)
    """
    snippet = item.get("snippet", {})
    channel = snippet.get("channelTitle", "").lower()
    title = snippet.get("title", "").lower()

    score = 0

    official_leagues = ["nba", "nfl", "nhl", "mlb", "mls", "serie b"]
    if any(league in channel for league in official_leagues):
        score += 3

    official_teams = ["celtics", "patriots", "bruins", "red sox", "revolution", "palermo"]
    if any(team in channel for team in official_teams):
        score += 2

    if "full" in title or "extended" in title:
        score += 1

    if "reaction" in title or "fan" in title:
        score -= 1

    return score


async def search_youtube_highlights(
        team_name: str,
        game_date: str,
        opponent: str) -> dict | None:
    """
    Search YouTube for highlight videos for a specific game.

    Returns a dict with video details if found, None otherwise.
    """
    if not YOUTUBE_API_KEY:
        logger.error("YOUTUBE_API_KEY not set")
        return None

    # Instead of having a date in the query, publishedAfter should filter videos correctly.
    # As, video titles use inconsistent formats.
    query = f"{team_name} vs {opponent} highlights"

    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": 5,
        "order": "relevance",
        "publishedAfter": f"{game_date}T00:00:00Z",
        "key": YOUTUBE_API_KEY
    }

    # Retry the YouTube search API call with exponential backoff
    data = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(YOUTUBE_SEARCH_URL, params=params)
                response.raise_for_status()
                data = response.json()
            break
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                logger.error(f"YouTube API client error for {team_name}: {e}")
                return None
            logger.warning(
                f"YouTube search retry {attempt}/{MAX_RETRIES} for {team_name}: {e}"
            )
        except httpx.HTTPError as e:
            logger.warning(
                f"YouTube search retry {attempt}/{MAX_RETRIES} for {team_name}: {e}"
            )
        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_BACKOFF_FACTOR * 2 ** (attempt - 1))
    else:
        logger.error(f"YouTube search failed after {MAX_RETRIES} retries for {team_name}")
        return None

    if data is None:
        return None

    try:
        items = data.get("items", [])
        if not items:
            logger.warning(f"No YouTube results found for: {query}")
            return None

        video_ids = [
            item.get("id", {}).get("videoId")
            for item in items
            if item.get("id", {}).get("videoId")
        ]
        durations = {}
        if video_ids:
            async with httpx.AsyncClient(timeout=10.0) as client:
                videos_response = await client.get(YOUTUBE_VIDEOS_URL, params={
                    "part": "contentDetails",
                    "id": ",".join(video_ids),
                    "key": YOUTUBE_API_KEY
                })
                videos_response.raise_for_status()
                for v in videos_response.json().get("items", []):
                    vid_id = v.get("id")
                    iso_duration = v.get(
                        "contentDetails", {}).get("duration", "PT0S")
                    durations[vid_id] = _parse_iso8601_duration(iso_duration)

        is_long_enough = [
            item for item in items
            if durations.get(item.get("id", {}).get("videoId"), 0) >= MIN_DURATION_SECONDS
        ]

        if not is_long_enough:
            logger.warning(
                f"No YouTube results over {MIN_DURATION_SECONDS // 60} minutes found for: {query}"
            )
            return None

        scored = sorted(is_long_enough, key=_score_youtube_result, reverse=True)
        best = scored[0]

        video_id = best.get("id", {}).get("videoId")
        snippet = best.get("snippet", {})

        if not video_id:
            return None

        logger.info(
            f"Selected highlight for {team_name}: '"
            f"{snippet.get('title')}' from '"
            f"{snippet.get('channelTitle')}' "
            f"(score: {_score_youtube_result(best)}, duration: "
            f"{durations.get(video_id)}s)"
        )

        return {
            "title": snippet.get("title", "Highlights"),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "channel": snippet.get("channelTitle", ""),
            "published_at": snippet.get("publishedAt", "")[:10],
        }

    except httpx.HTTPError as e:
        logger.error(f"YouTube video details API error for {team_name}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error searching YouTube: {e}")
        return None


class SportsDataManager:
    """
    Orchestrates ESPN and YouTube API calls to produce highlight results.
    """

    async def get_teams_that_played(
            self, game_date: str | None = None) -> list[str]:
        """
        Check ESPN for each tracked team and return those that played
        on the given date (defaults to yesterday).

        Args:
            game_date: YYYY-MM-DD format. Defaults to yesterday if omitted.

        Returns:
            List of team names that had a game on that date.
        """
        target_date = game_date or str(date.today() - timedelta(days=1))
        espn_date = target_date.replace("-", "")

        logger.info(f"Checking ESPN for games on {target_date}")

        # Cache ESPN responses by league to avoid duplicate API calls
        leagues_checked = {}
        teams_that_played = []

        for team_name, config in TRACKED_TEAMS.items():
            league_key = f"{config['sport']}/{config['league']}"

            if league_key not in leagues_checked:
                espn_data = await fetch_espn_scoreboard(
                    config['sport'], config['league'], espn_date
                )
                leagues_checked[league_key] = espn_data

            espn_data = leagues_checked[league_key]
            game = extract_game_from_espn(espn_data, config['espn_name'])

            if game:
                teams_that_played.append(team_name)
                logger.info(
                    f"Found game: {team_name} vs {
                        game['opponent']} ({
                        game['result']})")

        return sorted(teams_that_played)

    async def get_highlights(
        self,
        team: str | None = None,
        game_date: str | None = None
    ) -> list[dict]:
        """
        Get game results and YouTube highlight URLs for teams that played.

        Args:
            team:      Filter to a specific team. If None, checks all tracked teams.
            game_date: YYYY-MM-DD format. Defaults to yesterday if omitted.

        Returns:
            List of highlight dicts, each containing game result and YouTube URL.
        """
        target_date = game_date or str(date.today() - timedelta(days=1))
        espn_date = target_date.replace("-", "")

        teams_to_check = (
            {team: TRACKED_TEAMS[team]}
            if team and team in TRACKED_TEAMS
            else TRACKED_TEAMS
        )

        results = []
        leagues_checked = {}

        for team_name, config in teams_to_check.items():
            league_key = f"{config['sport']}/{config['league']}"

            if league_key not in leagues_checked:
                espn_data = await fetch_espn_scoreboard(
                    config['sport'], config['league'], espn_date
                )
                leagues_checked[league_key] = espn_data

            espn_data = leagues_checked[league_key]
            game = extract_game_from_espn(espn_data, config['espn_name'])

            if not game:
                logger.info(f"No game found for {team_name} on {target_date}")
                continue

            logger.info(f"Searching YouTube for {team_name} highlights...")
            highlight = await search_youtube_highlights(
                team_name,
                target_date,
                game['opponent']
            )

            results.append({
                "team": team_name,
                "opponent": game['opponent'],
                "score": game['score'],
                "result": game['result'],
                "game_date": target_date,
                "highlight_title": highlight['title'] if highlight else None,
                "highlight_url": highlight['url'] if highlight else None,
                "highlight_channel": highlight['channel'] if highlight else None,
                "highlight_found": highlight is not None
            })

        return results

    def get_stats(self) -> dict:
        return {
            "tracked_teams": list(TRACKED_TEAMS.keys()),
            "team_count": len(TRACKED_TEAMS),
            "sports_covered": list({v['league'].upper() for v in TRACKED_TEAMS.values()}),
            "data_sources": ["ESPN unofficial API", "YouTube Data API v3"],
            "note": "Checks yesterday's games by default"
        }


server = MCPServer("sports_highlights_server")
data_manager = SportsDataManager()


@server.tool(
    name="sports_get_highlights",
    description=(
        "Get yesterday's game results and YouTube highlight URLs "
        "for tracked teams (New England sports teams and Palermo F.C.). "
        "Returns score, result, and a YouTube link for each team that played."
    ),
)
async def handle_get_highlights(
    team: str | None = None,
    game_date: str | None = None,
) -> str:
    """Get highlights for New England sports teams."""
    try:
        results = await data_manager.get_highlights(team=team, game_date=game_date)

        if not results:
            return "No tracked teams played yesterday."

        return json.dumps(results, indent=2)
    except Exception as e:
        logger.error("Error in sports_get_highlights: %s", e, exc_info=True)
        return f"Error executing tool: {e}"


@server.tool(
    name="sports_get_teams_that_played",
    description=(
        "Check which tracked teams had games yesterday "
        "(New England sports teams and Palermo F.C.). "
        "Returns a list of team names. "
        "Use this first if you want to know whether any teams played "
        "before fetching full highlight details."
    ),
)
async def handle_get_teams_that_played(
    game_date: str | None = None,
) -> str:
    """Check which New England teams had games."""
    try:
        teams = await data_manager.get_teams_that_played(game_date=game_date)

        if not teams:
            return "No tracked teams played yesterday."

        return json.dumps(teams, indent=2)
    except Exception as e:
        logger.error("Error in sports_get_teams_that_played: %s", e, exc_info=True)
        return f"Error executing tool: {e}"


@server.tool(
    name="sports_get_dataset_stats",
    description="Get an overview of which teams and sports this server covers.",
)
async def handle_get_dataset_stats() -> str:
    """Get an overview of which teams and sports this server covers."""
    try:
        return json.dumps(data_manager.get_stats(), indent=2)
    except Exception as e:
        logger.error("Error in sports_get_dataset_stats: %s", e, exc_info=True)
        return f"Error executing tool: {e}"


def main():
    logger.info("Sports Highlights MCP Server starting...")
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
