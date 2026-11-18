"""Optional Exa and Firecrawl research tools for the local MCP workspace."""
import ipaddress
import os
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("ai-tools-workspace-research")
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _validate_public_url(url: str) -> str:
    cleaned = url.strip()
    parsed = urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must be an absolute http or https URL.")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("Localhost URLs are not allowed.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_reserved or address.is_multicast or address.is_unspecified
    ):
        raise ValueError("Private, local, and reserved IP addresses are not allowed.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing embedded credentials are not allowed.")
    return cleaned


def _normalize_exa_results(payload: dict) -> list[dict]:
    return [
        {
            "title": item.get("title") or "Untitled",
            "url": item.get("url", ""),
            "snippet": item.get("text") or (item.get("highlights") or [""])[0],
            "published_date": item.get("publishedDate"),
            "source": "Exa",
        }
        for item in payload.get("results", [])
    ]


def _normalize_firecrawl_results(payload: dict) -> list[dict]:
    data = payload.get("data", [])
    if isinstance(data, dict):
        data = data.get("web", []) + data.get("news", [])
    normalized = []
    for item in data:
        metadata = item.get("metadata") or {}
        normalized.append({
            "title": item.get("title") or metadata.get("title") or "Untitled",
            "url": item.get("url") or metadata.get("sourceURL") or "",
            "snippet": item.get("description") or metadata.get("description") or "",
            "published_date": item.get("publishedDate") or metadata.get("publishedTime"),
            "source": "Firecrawl",
        })
    return normalized


async def search_web(query: str, num_results: int = 5) -> list[dict]:
    cleaned = query.strip()
    if not cleaned:
        raise ValueError("Search query must not be empty.")
    if not 1 <= num_results <= 10:
        raise ValueError("num_results must be between 1 and 10.")
    exa_key = os.getenv("EXA_API_KEY", "").strip()
    firecrawl_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    if exa_key:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(
                "https://api.exa.ai/search",
                headers={"x-api-key": exa_key, "Content-Type": "application/json"},
                json={"query": cleaned, "type": "auto", "numResults": num_results,
                      "contents": {"text": True}},
            )
            response.raise_for_status()
            return _normalize_exa_results(response.json())
    if firecrawl_key:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(
                "https://api.firecrawl.dev/v2/search",
                headers={"Authorization": f"Bearer {firecrawl_key}"},
                json={"query": cleaned, "limit": num_results},
            )
            response.raise_for_status()
            return _normalize_firecrawl_results(response.json())
    raise RuntimeError(
        "Web research is not configured. Add EXA_API_KEY or FIRECRAWL_API_KEY "
        "to the private .env file. No paid billing is required for the documented free tiers."
    )


async def scrape_page(url: str) -> dict:
    target = _validate_public_url(url)
    firecrawl_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    if not firecrawl_key:
        raise RuntimeError(
            "Page scraping requires FIRECRAWL_API_KEY in the private .env file. "
            "Search can use EXA_API_KEY independently."
        )
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(
            "https://api.firecrawl.dev/v2/scrape",
            headers={"Authorization": f"Bearer {firecrawl_key}"},
            json={"url": target, "formats": ["markdown"], "onlyMainContent": True},
        )
        response.raise_for_status()
        payload = response.json()
    data = payload.get("data") or {}
    metadata = data.get("metadata") or {}
    markdown = data.get("markdown") or ""
    if not markdown:
        raise RuntimeError("Firecrawl returned no page markdown.")
    return {
        "url": metadata.get("sourceURL") or target,
        "title": metadata.get("title") or "",
        "markdown": markdown[:30000],
        "truncated": len(markdown) > 30000,
        "source": "Firecrawl",
    }


@mcp.tool()
async def web_search(query: str, num_results: int = 5) -> list[dict]:
    """Search the public web with Exa, or Firecrawl if Exa is not configured."""
    return await search_web(query, num_results)


@mcp.tool()
async def scrape_url(url: str) -> dict:
    """Extract main-page Markdown from a public HTTP(S) URL using Firecrawl."""
    return await scrape_page(url)


@mcp.tool()
def research_provider_status() -> dict:
    """Report configured research providers without revealing credential values."""
    exa = bool(os.getenv("EXA_API_KEY", "").strip())
    firecrawl = bool(os.getenv("FIRECRAWL_API_KEY", "").strip())
    if exa and firecrawl:
        status = "Exa search and Firecrawl search/scraping available"
    elif exa:
        status = "Exa search available; Firecrawl scraping unavailable"
    elif firecrawl:
        status = "Firecrawl search and scraping available"
    else:
        status = "No research provider configured"
    return {"exa_configured": exa, "firecrawl_configured": firecrawl, "status": status}


if __name__ == "__main__":
    mcp.run(transport="stdio")
