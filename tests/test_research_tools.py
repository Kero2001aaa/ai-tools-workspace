"""Offline tests for research provider normalization and URL safeguards."""
import unittest
from unittest.mock import patch

from agent.research_mcp_server import (
    _normalize_exa_results, _normalize_firecrawl_results,
    _validate_public_url, research_provider_status, search_web, scrape_page,
)


class ResearchToolTests(unittest.IsolatedAsyncioTestCase):
    def test_normalizes_exa_results(self):
        results = _normalize_exa_results({"results": [
            {"title": "Docs", "url": "https://example.com", "text": "Snippet"}
        ]})
        self.assertEqual(results[0]["source"], "Exa")
        self.assertEqual(results[0]["snippet"], "Snippet")

    def test_normalizes_firecrawl_results(self):
        results = _normalize_firecrawl_results({"data": [
            {"title": "Docs", "url": "https://example.com", "description": "Snippet"}
        ]})
        self.assertEqual(results[0]["source"], "Firecrawl")
        self.assertEqual(results[0]["url"], "https://example.com")

    def test_rejects_local_and_credential_urls(self):
        urls = ["http://localhost/admin", "http://127.0.0.1/",
                "http://192.168.1.10/", "https://user:password@example.com/",
                "file:///etc/passwd"]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                _validate_public_url(url)

    def test_accepts_public_http_url(self):
        self.assertEqual(_validate_public_url("https://example.com/docs"),
                         "https://example.com/docs")

    async def test_search_requires_a_configured_provider(self):
        with patch.dict("os.environ", {"EXA_API_KEY": "", "FIRECRAWL_API_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                await search_web("LangGraph")

    async def test_scrape_requires_firecrawl_key(self):
        with patch.dict("os.environ", {"FIRECRAWL_API_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "FIRECRAWL_API_KEY"):
                await scrape_page("https://example.com")

    def test_provider_status_never_exposes_keys(self):
        with patch.dict("os.environ", {"EXA_API_KEY": "secret-exa",
                                       "FIRECRAWL_API_KEY": ""}):
            status = research_provider_status()
        self.assertTrue(status["exa_configured"])
        self.assertFalse(status["firecrawl_configured"])
        self.assertNotIn("secret-exa", str(status))


if __name__ == "__main__":
    unittest.main()
