"""DuckDuckGo web search tool for the searcher agent."""

from ddgs import DDGS


def web_search(query: str) -> str:
    """
    Search the web using DuckDuckGo and return top results.

    Args:
        query: The search query.

    Returns:
        Formatted string of top results with title, URL, and snippet.
    """
    try:
        ddgs = DDGS()
        hits = list(ddgs.text(query, max_results=6))
        if not hits:
            return f"No results found for: {query}"
        lines = []
        for i, h in enumerate(hits, 1):
            lines.append(f"{i}. {h.get('title', '')}")
            lines.append(f"   URL: {h.get('href', '')}")
            lines.append(f"   {h.get('body', '')}")
        return "\n".join(lines)
    except Exception as e:
        return f"Search failed: {e}"
