"""Optional Exa and Firecrawl research tools for the local MCP workspace."""
import asyncio
import json
import ipaddress
import hashlib
import os
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("ai-tools-workspace-research")
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _validate_unicode(value):
    """Reject lone surrogates even in metadata before MCP serializes the result."""
    if isinstance(value, str):
        try:
            value.encode('utf-8')
        except UnicodeError:
            raise ValueError('research_response_invalid') from None
    elif isinstance(value, dict):
        for key, item in value.items():
            _validate_unicode(key)
            _validate_unicode(item)
    elif isinstance(value, list):
        for item in value:
            _validate_unicode(item)


def _validate_public_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("URL must be bounded text")
    _validate_unicode(url)
    cleaned = url.strip()
    if any(ord(character) <= 32 or ord(character) == 127 for character in cleaned):
        raise ValueError("URL contains unsupported whitespace or controls")
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


MAX_RESULTS = 10
MAX_SOURCE_TEXT = 12000
MAX_SOURCE_TITLE = 300
MAX_URL = 2048


def _source_record(item, provider):
    if not isinstance(item, dict):
        raise ValueError("Invalid research result")
    _validate_unicode(item)
    metadata = item.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("Invalid research metadata")
    for container, fields in ((item, ('url', 'title', 'text', 'description')), (metadata, ('sourceURL', 'title', 'description'))):
        if any(field in container and container[field] is not None and not isinstance(container[field], str) for field in fields):
            raise ValueError("Invalid research text field")
    url = item.get("url") or metadata.get("sourceURL") or ""
    if not isinstance(url, str) or len(url) > MAX_URL:
        raise ValueError("Invalid research source URL")
    url = _validate_public_url(url)
    title = item.get("title") or metadata.get("title") or "Untitled"
    highlights = item.get("highlights") or []
    if not isinstance(highlights, list):
        raise ValueError("Invalid research highlights")
    text = item.get("text") or item.get("description") or metadata.get("description") or (highlights[0] if highlights else "")
    if not isinstance(title, str) or not isinstance(text, str):
        raise ValueError("Invalid research source text")
    excerpt = text[:MAX_SOURCE_TEXT]
    try:
        excerpt.encode('utf-8')
        title.encode('utf-8')
        url.encode('utf-8')
    except UnicodeError:
        raise ValueError('research_response_invalid') from None
    published = item.get('publishedDate') or metadata.get('publishedTime')
    return {"title": title[:MAX_SOURCE_TITLE], "url": url, "snippet": excerpt,
            "published_date": published[:64] if isinstance(published, str) else None,
            "source": provider, "truncated": len(text) > MAX_SOURCE_TEXT or len(title) > MAX_SOURCE_TITLE,
            "evidence_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest()}


def _normalize_exa_results(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("results", []), list):
        raise ValueError("Invalid Exa response")
    return [_source_record(item, "Exa") for item in payload.get("results", [])[:MAX_RESULTS]]


def _normalize_firecrawl_results(payload):
    if not isinstance(payload, dict):
        raise ValueError("Invalid Firecrawl response")
    data = payload.get("data", [])
    if isinstance(data, dict):
        web, news = data.get("web", []), data.get("news", [])
        if not isinstance(web, list) or not isinstance(news, list):
            raise ValueError("Invalid Firecrawl result groups")
        data = web[:MAX_RESULTS] + news[:MAX_RESULTS]
    if not isinstance(data, list):
        raise ValueError("Invalid Firecrawl results")
    return [_source_record(item, "Firecrawl") for item in data[:MAX_RESULTS]]


MAX_PROVIDER_BYTES = 512 * 1024
_FIXED_ENDPOINTS = frozenset({'https://api.exa.ai/search',
    'https://api.firecrawl.dev/v2/search', 'https://api.firecrawl.dev/v2/scrape'})


def _json_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('research_response_invalid')
        result[key] = value
    return result


async def _provider_json(endpoint, headers, body):
    """Fixed-provider, identity-only, streamed bounded JSON; one request, no retry.

    Compression is refused before body reading to avoid decompression allocation
    outside the cap. UTF-8 only, duplicate keys/nonfinite JSON fail closed.
    """
    if endpoint not in _FIXED_ENDPOINTS:
        raise ValueError('research_endpoint_invalid')
    async def read():
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
            async with client.stream('POST', endpoint,
                    headers={**headers, 'Accept': 'application/json', 'Accept-Encoding': 'identity'}, json=body) as response:
                if response.status_code != 200:
                    raise RuntimeError('research_provider_unavailable')
                if response.headers.get('content-encoding', 'identity').strip().lower() != 'identity':
                    raise ValueError('research_encoding_unsupported')
                if response.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
                    raise ValueError('research_media_invalid')
                length = response.headers.get('content-length')
                if length is not None and (not length.isascii() or not length.isdigit() or len(length) > 9 or int(length) > MAX_PROVIDER_BYTES):
                    raise ValueError('research_response_too_large')
                buffer = bytearray()
                async for chunk in response.aiter_raw(chunk_size=8192):
                    if len(buffer) + len(chunk) > MAX_PROVIDER_BYTES:
                        raise ValueError('research_response_too_large')
                    buffer.extend(chunk)
                try:
                    def nonfinite(value):
                        raise ValueError('research_response_invalid')
                    payload = json.loads(buffer.decode('utf-8'), object_pairs_hook=_json_pairs, parse_constant=nonfinite)
                except (ValueError, UnicodeError, RecursionError):
                    raise ValueError('research_response_invalid') from None
                if not isinstance(payload, dict):
                    raise ValueError('research_response_invalid')
                try:
                    _validate_unicode(payload)
                except RecursionError:
                    raise ValueError("research_response_invalid") from None
                return payload
    try:
        return await asyncio.wait_for(read(), timeout=35)
    except (httpx.HTTPError, OSError, asyncio.TimeoutError):
        raise RuntimeError('research_transport_unavailable') from None


async def search_web(query: str, num_results: int = 5) -> list[dict]:
    if not isinstance(query, str) or len(query) > 2000:
        raise ValueError("Search query must be bounded text")
    _validate_unicode(query)
    cleaned = query.strip()
    if not cleaned:
        raise ValueError("Search query must not be empty.")
    if type(num_results) is not int or not 1 <= num_results <= 10:
        raise ValueError("num_results must be between 1 and 10.")
    exa_key = os.getenv("EXA_API_KEY", "").strip()
    firecrawl_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    if exa_key:
        payload = await _provider_json('https://api.exa.ai/search',
            {'x-api-key': exa_key, 'Content-Type': 'application/json'},
            {'query': cleaned, 'type': 'auto', 'numResults': num_results, 'contents': {'text': True}})
        return _normalize_exa_results(payload)[:num_results]
    if firecrawl_key:
        payload = await _provider_json('https://api.firecrawl.dev/v2/search',
            {'Authorization': 'Bearer ' + firecrawl_key}, {'query': cleaned, 'limit': num_results})
        return _normalize_firecrawl_results(payload)[:num_results]
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
    payload = await _provider_json('https://api.firecrawl.dev/v2/scrape',
        {'Authorization': 'Bearer ' + firecrawl_key},
        {'url': target, 'formats': ['markdown'], 'onlyMainContent': True})
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("Invalid scrape response")
    _validate_unicode(payload)
    data = payload["data"]
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("Invalid scrape metadata")
    markdown = data.get("markdown") or ""
    if not isinstance(markdown, str) or not markdown:
        raise RuntimeError("Firecrawl returned no page markdown.")
    reported = _validate_public_url(metadata.get("sourceURL") or target)
    try:
        markdown[:30000].encode('utf-8')
        reported.encode('utf-8')
    except UnicodeError:
        raise ValueError('research_response_invalid') from None
    return {
        "url": target,
        "requested_url": target,
        "reported_url": reported,
        "source_url_matches_request": reported == target,
        "title": (metadata.get("title") or "")[:MAX_SOURCE_TITLE] if isinstance(metadata.get("title") or "", str) else "",
        "markdown": markdown[:30000],
        "truncated": len(markdown) > 30000,
        "source": "Firecrawl",
        "evidence_sha256": hashlib.sha256(markdown[:30000].encode("utf-8")).hexdigest(),
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
