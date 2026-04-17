"""Web search tool using DuckDuckGo HTML search."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_DDG_URL = "https://html.duckduckgo.com/html/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}
_TIMEOUT = 10.0


class SearchTool:
    """Web search via DuckDuckGo HTML interface.

    No API key required. Parses result titles, URLs, and snippets from
    the lightweight HTML endpoint.
    """

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Search the web for current information. Use this when the user asks "
            "about recent events, facts you're unsure about, or anything that "
            "benefits from up-to-date web results."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 5).",
                },
            },
            "required": ["query"],
        }

    async def execute(self, *, query: str, limit: int = 5, **_: Any) -> str:
        return await self.search(query, limit=limit)

    async def search(self, query: str, *, limit: int = 5) -> str:
        """Run a DuckDuckGo search and return formatted results."""
        try:
            results = await self._fetch_results(query, limit)
        except Exception:
            logger.exception("DuckDuckGo search failed for query: %s", query)
            return f"Search failed for: {query}. A proper search API should be configured."

        if not results:
            return f"No results found for: {query}"

        lines = [f"Search results for: {query}\n"]
        for i, (title, snippet, url) in enumerate(results, 1):
            lines.append(f"{i}. {title}")
            if snippet:
                lines.append(f"   {snippet}")
            if url:
                lines.append(f"   {url}")
            lines.append("")

        return "\n".join(lines).strip()

    async def _fetch_results(
        self, query: str, limit: int
    ) -> list[tuple[str, str, str]]:
        """Fetch and parse DuckDuckGo HTML results.

        Returns list of (title, snippet, url) tuples.
        """
        async with httpx.AsyncClient(
            headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True
        ) as client:
            resp = await client.post(_DDG_URL, data={"q": query, "b": ""})
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        results: list[tuple[str, str, str]] = []

        # DuckDuckGo HTML results live in <div class="result"> blocks
        for result_div in soup.select(".result"):
            if len(results) >= limit:
                break

            # Title link
            title_tag = result_div.select_one(".result__a")
            title = title_tag.get_text(strip=True) if title_tag else ""
            if not title:
                continue

            # Snippet
            snippet_tag = result_div.select_one(".result__snippet")
            snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""

            # URL — the displayed URL text, not the DDG redirect
            url_tag = result_div.select_one(".result__url")
            url = url_tag.get_text(strip=True) if url_tag else ""
            if url and not url.startswith("http"):
                url = "https://" + url

            results.append((title, snippet, url))

        return results
