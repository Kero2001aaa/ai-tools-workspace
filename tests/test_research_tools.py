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

    def test_bounded_source_evidence_digest_and_count(self):
        import hashlib
        item = {'title': 'T' * 500, 'url': 'https://example.com/source', 'text': 'x' * 20000}
        results = _normalize_exa_results({'results': [item] * 20})
        self.assertEqual(10, len(results))
        self.assertEqual(12000, len(results[0]['snippet']))
        self.assertEqual(300, len(results[0]['title']))
        self.assertTrue(results[0]['truncated'])
        self.assertEqual(hashlib.sha256(('x' * 12000).encode()).hexdigest(), results[0]['evidence_sha256'])

    def test_malformed_and_unsafe_sources_fail_closed(self):
        for payload in ({'results': {}}, {'results': [None]}, {'results': [{'url': 'http://127.0.0.1'}]},
                        {'results': [{'url': 'https://example.com', 'text': {}}]},
                        {'results': [{'url': 'https://example.com', 'highlights': 'bad'}]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _normalize_exa_results(payload)
        for payload in ([], {'data': {'web': 'bad'}}, {'data': [None]}):
            with self.assertRaises(ValueError):
                _normalize_firecrawl_results(payload)

    async def test_invalid_search_bounds_never_create_client(self):
        with patch('agent.research_mcp_server.httpx.AsyncClient', side_effect=AssertionError('no network')):
            for query, limit in ((None, 1), ('x' * 2001, 1), ('valid', True), ('valid', 11)):
                with self.assertRaises(ValueError):
                    await search_web(query, limit)


if __name__ == "__main__":
    unittest.main()
