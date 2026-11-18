"""Offline tests for browser URL restrictions."""
import unittest

from agent.browser_mcp_server import validate_browser_url


class BrowserUrlTests(unittest.TestCase):
    def test_accepts_public_http_and_https(self):
        self.assertEqual(
            validate_browser_url("https://example.com/docs"),
            "https://example.com/docs",
        )
        self.assertEqual(
            validate_browser_url("http://example.org/page"),
            "http://example.org/page",
        )

    def test_rejects_local_and_private_destinations(self):
        urls = [
            "http://localhost/",
            "http://127.0.0.1/",
            "http://10.0.0.8/",
            "http://192.168.1.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
            "https://service.localhost/",
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_browser_url(url)

    def test_rejects_non_http_schemes_and_credentials(self):
        urls = [
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:text/html,hello",
            "https://user:password@example.com/",
            "/relative/path",
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_browser_url(url)


if __name__ == "__main__":
    unittest.main()
