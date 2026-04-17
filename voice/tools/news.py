"""News tool — fetches headlines from RSS feeds via feedparser."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser

logger = logging.getLogger(__name__)

DEFAULT_FEEDS: dict[str, str] = {
    "BBC World": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Reuters": "https://www.rss-bridge.org/bridge01/?action=display&bridge=Reuters&feed=world&format=Atom",
    "CBC News": "https://www.cbc.ca/webfeed/rss/rss-world",
    "AP News": "https://rsshub.app/apnews/topics/apf-topnews",
}


@dataclass
class NewsConfig:
    """Configuration for the news tool."""
    feeds: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_FEEDS))
    request_timeout: int = 15


def _parse_published(entry: Any) -> datetime | None:
    """Extract a timezone-aware datetime from a feed entry."""
    raw = getattr(entry, "published", None) or getattr(entry, "updated", None)
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (ValueError, TypeError):
        pass
    # Atom feeds may use ISO-8601 instead of RFC-2822.
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _fetch_feed(url: str, timeout: int) -> feedparser.FeedParserDict:
    """Synchronous feed parse — runs in a thread executor."""
    # feedparser handles its own HTTP request. Setting a timeout via
    # the underlying urllib handler isn't directly supported, but the
    # request_headers kwarg lets us keep the call clean. A socket-level
    # default timeout is applied below.
    import socket
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        return feedparser.parse(url)
    finally:
        socket.setdefaulttimeout(old_timeout)


class NewsTool:
    """Aggregates headlines from multiple RSS/Atom feeds."""

    def __init__(self, config: NewsConfig | None = None) -> None:
        self._config = config or NewsConfig()
        self._executor = ThreadPoolExecutor(
            max_workers=len(self._config.feeds),
            thread_name_prefix="news-feed",
        )

    def close(self) -> None:
        """Shut down the thread pool."""
        self._executor.shutdown(wait=False)

    async def get_headlines(self, limit: int = 5) -> list[dict[str, Any]]:
        """Fetch top headlines across all configured feeds.

        Returns a list of dicts: {title, source, url, published}.
        Results are sorted by publication date (newest first).
        """
        loop = asyncio.get_running_loop()
        tasks = [
            loop.run_in_executor(
                self._executor,
                _fetch_feed,
                url,
                self._config.request_timeout,
            )
            for url in self._config.feeds.values()
        ]
        source_names = list(self._config.feeds.keys())

        results = await asyncio.gather(*tasks, return_exceptions=True)

        headlines: list[dict[str, Any]] = []
        for source_name, result in zip(source_names, results):
            if isinstance(result, Exception):
                logger.warning("Failed to fetch feed %s: %s", source_name, result)
                continue
            feed: feedparser.FeedParserDict = result
            if feed.bozo and not feed.entries:
                logger.warning(
                    "Feed %s returned no entries (bozo: %s)",
                    source_name,
                    feed.bozo_exception,
                )
                continue
            for entry in feed.entries:
                title = getattr(entry, "title", None)
                if not title:
                    continue
                published = _parse_published(entry)
                headlines.append({
                    "title": title.strip(),
                    "source": source_name,
                    "url": getattr(entry, "link", ""),
                    "published": published.isoformat() if published else None,
                    "_sort_key": published or datetime.min.replace(tzinfo=timezone.utc),
                })

        # Sort newest-first, then trim.
        headlines.sort(key=lambda h: h["_sort_key"], reverse=True)
        for h in headlines:
            del h["_sort_key"]

        return headlines[:limit]

    async def get_summary(self) -> str:
        """Return a voice-friendly summary of the top headlines."""
        headlines = await self.get_headlines(limit=5)
        if not headlines:
            return "I wasn't able to fetch any news headlines right now."

        lines = [f"Here are the top {len(headlines)} headlines:"]
        for i, h in enumerate(headlines, 1):
            source = h["source"]
            title = h["title"]
            lines.append(f"{i}. From {source}: {title}")

        return " ".join(lines)
