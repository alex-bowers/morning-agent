"""
Wishlist Price Watch
=====================
Checks a public Amazon UK wishlist and reports every item priced below a
single target price (e.g. £1.00).

The wishlist page renders the first batch of items and a "show more" URL that
the browser fetches as you scroll. We follow those URLs directly with httpx
until Amazon marks the end of the list, so no headless browser is needed.

Daily usage (in agent.py):
  1. await find_items_below_target(wishlist_id, target_price)
  2. If anything is returned, post format_price_alert(...) to Slack.

No state is kept between runs, so an item is reported every day while its
price stays below the target.
"""

import asyncio
import html
import logging
import re
from decimal import Decimal

import httpx

logger = logging.getLogger("morning_agent.price_watch")

AMAZON_BASE_URL = "https://www.amazon.co.uk"

# Amazon returns 503 to requests that don't look like a browser. The Accept
# header is required, and httpx's default Accept-Encoding and Connection
# headers must be removed (see DROPPED_DEFAULT_HEADERS).
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
DROPPED_DEFAULT_HEADERS = ("Accept-Encoding", "Connection")

MAX_PAGES = 20  # Safety cap — a page holds ~10 items
PAGE_DELAY_SECONDS = 2
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 15  # doubles each retry; Amazon rate limits need longer waits

WISHLIST_ID_PATTERN = re.compile(r"^[A-Z0-9]+$")
ASIN_PATTERN = re.compile(r"itemExternalId&quot;:&quot;ASIN:([A-Z0-9]{10})")
TITLE_PATTERN = re.compile(r'title="([^"]+)"')
PRICE_PATTERN = re.compile(r'a-offscreen">£([\d,]+\.\d{2})')
SHOW_MORE_PATTERN = re.compile(r'value="([^"]+)" class="showMoreUrl"')
END_OF_LIST_MARKER = "endOfListMarker"
CAPTCHA_MARKER = "validateCaptcha"


class WishlistFetchError(Exception):
    """Raised when the wishlist cannot be fetched or parsed."""


async def fetch_page(client: httpx.AsyncClient, url: str) -> str:
    """
    Fetch one wishlist page. Retries server errors (5xx) and network errors
    with exponential backoff; client errors (4xx) fail immediately.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                msg = f"Amazon returned {e.response.status_code} for the wishlist"
                raise WishlistFetchError(msg) from e
            logger.warning("Wishlist retry %d/%d: %s", attempt, MAX_RETRIES, e)
        except httpx.HTTPError as e:
            logger.warning("Wishlist retry %d/%d: %s", attempt, MAX_RETRIES, e)
        else:
            if CAPTCHA_MARKER in response.text:
                msg = "Amazon served a CAPTCHA instead of the wishlist"
                raise WishlistFetchError(msg)
            return response.text

        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1))

    msg = f"Amazon did not respond successfully after {MAX_RETRIES} attempts"
    raise WishlistFetchError(msg)


def parse_items(page_html: str) -> list[dict]:
    """
    Extract items from a wishlist page.

    Each ASIN appears several times within its item's markup, so an item's
    block runs from the first mention of its ASIN to the first mention of the
    next ASIN.

    Returns:
        List of dicts with asin, title and price (Decimal, or None when the
        item has no current price, e.g. it is unavailable).
    """
    starts: list[tuple[int, str]] = []
    seen: set[str] = set()
    for match in ASIN_PATTERN.finditer(page_html):
        asin = match.group(1)
        if asin not in seen:
            seen.add(asin)
            starts.append((match.start(), asin))

    items = []
    for index, (position, asin) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(page_html)
        block = page_html[position:end]
        title_match = TITLE_PATTERN.search(block)
        price_match = PRICE_PATTERN.search(block)
        items.append(
            {
                "asin": asin,
                "title": html.unescape(title_match.group(1)) if title_match else asin,
                "price": Decimal(price_match.group(1).replace(",", "")) if price_match else None,
            }
        )
    return items


def next_page_url(page_html: str) -> str | None:
    """Return the absolute URL of the next page, or None at the end of the list."""
    if END_OF_LIST_MARKER in page_html:
        return None

    match = SHOW_MORE_PATTERN.search(page_html)
    if not match:
        return None

    path = html.unescape(match.group(1))
    # Only follow relative Amazon paths, never an absolute URL to another host
    if not path.startswith("/") or path.startswith("//"):
        logger.warning("Ignoring unexpected wishlist pagination URL: %s", path)
        return None
    return AMAZON_BASE_URL + path


async def fetch_wishlist(wishlist_id: str) -> list[dict]:
    """Fetch every item on the wishlist, following pagination to the end."""
    if not WISHLIST_ID_PATTERN.match(wishlist_id):
        msg = f"WISHLIST_ID {wishlist_id!r} is not a valid Amazon list ID"
        raise WishlistFetchError(msg)

    items: dict[str, dict] = {}
    url: str | None = f"{AMAZON_BASE_URL}/hz/wishlist/ls/{wishlist_id}"
    pages = 0

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        for header in DROPPED_DEFAULT_HEADERS:
            client.headers.pop(header, None)
        client.headers.update(REQUEST_HEADERS)

        while url and pages < MAX_PAGES:
            if pages:
                await asyncio.sleep(PAGE_DELAY_SECONDS)
            page_html = await fetch_page(client, url)
            pages += 1
            for item in parse_items(page_html):
                items.setdefault(item["asin"], item)
            url = next_page_url(page_html)

    if url:
        logger.warning("Stopped after %d wishlist pages; some items may be missing", MAX_PAGES)

    if not items:
        msg = "No items found on the wishlist — Amazon may have changed the page layout"
        raise WishlistFetchError(msg)

    logger.info("Fetched %d wishlist items across %d page(s)", len(items), pages)
    return list(items.values())


async def find_items_below_target(wishlist_id: str, target_price: Decimal) -> list[dict]:
    """Return wishlist items priced strictly below the target, cheapest first."""
    items = await fetch_wishlist(wishlist_id)

    unpriced = [item["asin"] for item in items if item["price"] is None]
    if unpriced:
        logger.info("No current price for %d item(s): %s", len(unpriced), ", ".join(unpriced))

    below = [item for item in items if item["price"] is not None and item["price"] < target_price]
    return sorted(below, key=lambda item: item["price"])


def _escape_slack(text: str) -> str:
    """Escape text for Slack mrkdwn, including link labels."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "-")


def format_price_alert(items: list[dict], target_price: Decimal) -> str:
    """Build the Slack message listing items below the target price."""
    noun = "item" if len(items) == 1 else "items"
    lines = [f"*{len(items)} wishlist {noun} under £{target_price:.2f}*"]
    for item in items:
        url = f"{AMAZON_BASE_URL}/dp/{item['asin']}"
        lines.append(f"• <{url}|{_escape_slack(item['title'])}> — £{item['price']:.2f}")
    return "\n".join(lines)
