"""Mock HTTP streams only, no provider traffic or credential activation."""
import json
import unittest
from unittest.mock import patch
import httpx
from agent import research_mcp_server as research


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk


class ResearchStreamTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, stream, *, status=200, headers=None):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(status, headers=headers or {'content-type': 'application/json'}, stream=stream)
        factory = httpx.AsyncClient
        def client(**kwargs):
            return factory(**kwargs, transport=httpx.MockTransport(handler))
        with patch.object(research.httpx, 'AsyncClient', side_effect=client):
            result = await research._provider_json('https://api.exa.ai/search', {'x-api-key': 'synthetic'}, {'query': 'safe'})
        return result, requests

    async def test_chunked_json_fixed_endpoint_and_identity_request(self):
        result, requests = await self.request(Chunks([b'{"results":', b'[]}']))
        self.assertEqual({'results': []}, result)
        self.assertEqual(1, len(requests))
        self.assertEqual('identity', requests[0].headers['accept-encoding'])

    async def test_byte_cap_without_length_stops_before_remaining_chunks(self):
        stream = Chunks([b'x' * 8192] * 1000)
        with self.assertRaisesRegex(ValueError, 'too_large'):
            await self.request(stream)
        self.assertLess(stream.reads, 1000)

    async def test_length_compression_media_and_status_refused_before_read(self):
        cases = [(200, {'content-type': 'application/json', 'content-length': '9999999'}),
                 (200, {'content-type': 'application/json', 'content-encoding': 'gzip'}),
                 (200, {'content-type': 'text/html'}), (429, {'content-type': 'application/json'}),
                 (307, {'content-type': 'application/json', 'location': 'https://other.invalid'})]
        for status, headers in cases:
            stream = Chunks([b'private-error-body'])
            with self.subTest(status=status, headers=headers), self.assertRaises((ValueError, RuntimeError)) as error:
                await self.request(stream, status=status, headers=headers)
            self.assertEqual(0, stream.reads)
            self.assertNotIn('private-error-body', str(error.exception))

    async def test_duplicate_nonfinite_deep_invalid_json_sanitized(self):
        for data in [b'{"a":1,"a":2}', b'{"x":NaN}', b'private not json', b'\xff', b'[]', b'[' * 2000]:
            with self.subTest(data=data[:12]), self.assertRaisesRegex(ValueError, '^research_response_invalid$'):
                await self.request(Chunks([data]))

    async def test_public_search_uses_stream_and_caps_returned_results(self):
        payload = {'results': [{'title': 'source', 'url': 'https://example.com', 'text': 'evidence'}] * 5}
        with patch.dict('os.environ', {'EXA_API_KEY': 'synthetic', 'FIRECRAWL_API_KEY': ''}), patch.object(research, '_provider_json', return_value=payload) as call:
            result = await research.search_web('bounded', 2)
        self.assertEqual(2, len(result))
        call.assert_awaited_once()

    async def test_unapproved_endpoint_never_opens_client(self):
        with patch.object(research.httpx, 'AsyncClient', side_effect=AssertionError('no client')):
            with self.assertRaisesRegex(ValueError, 'endpoint_invalid'):
                await research._provider_json('https://unapproved.invalid', {}, {})

    async def test_real_search_to_scrape_mock_stream_provenance_journey(self):
        import hashlib
        routes = []
        def handler(request):
            routes.append(str(request.url))
            if request.url.path == '/v2/search':
                payload = {'data': [{'title': 'Synthetic source', 'url': 'https://example.com/source', 'description': 'Excerpt'}]}
            else:
                self.assertEqual('https://example.com/source', json.loads(request.content)['url'])
                payload = {'data': {'markdown': 'Synthetic verified source body', 'metadata': {'sourceURL': 'https://example.com/source', 'title': 'Synthetic source'}}}
            return httpx.Response(200, headers={'content-type': 'application/json'}, stream=Chunks([json.dumps(payload).encode()]))
        factory = httpx.AsyncClient
        def client(**kwargs):
            return factory(**kwargs, transport=httpx.MockTransport(handler))
        with patch.dict('os.environ', {'EXA_API_KEY': '', 'FIRECRAWL_API_KEY': 'synthetic'}), patch.object(research.httpx, 'AsyncClient', side_effect=client):
            hits = await research.search_web('synthetic question', 1)
            source = await research.scrape_page(hits[0]['url'])
        self.assertEqual(['https://api.firecrawl.dev/v2/search', 'https://api.firecrawl.dev/v2/scrape'], routes)
        self.assertEqual(hits[0]['url'], source['url'])
        self.assertEqual(hashlib.sha256(source['markdown'].encode()).hexdigest(), source['evidence_sha256'])
        self.assertFalse(source['truncated'])

    async def test_scrape_rejects_unsafe_reported_source_after_mock_response(self):
        with patch.dict('os.environ', {'FIRECRAWL_API_KEY': 'synthetic'}), patch.object(research, '_provider_json', return_value={'data': {'markdown': 'text', 'metadata': {'sourceURL': 'http://127.0.0.1/private'}}}):
            with self.assertRaises(ValueError):
                await research.scrape_page('https://example.com/source')

    async def test_transport_error_does_not_return_private_exception(self):
        def handler(request):
            raise httpx.ConnectError('secret synthetic key in URL', request=request)
        factory = httpx.AsyncClient
        def client(**kwargs):
            return factory(**kwargs, transport=httpx.MockTransport(handler))
        with patch.object(research.httpx, 'AsyncClient', side_effect=client):
            with self.assertRaisesRegex(RuntimeError, '^research_transport_unavailable$'):
                await research._provider_json('https://api.exa.ai/search', {}, {})

    async def test_lone_surrogate_rejected_with_stable_code(self):
        payload = {'data': {'markdown': chr(0xd800), 'metadata': {'sourceURL': 'https://example.com'}}}
        with patch.dict('os.environ', {'FIRECRAWL_API_KEY': 'synthetic'}), patch.object(research, '_provider_json', return_value=payload):
            with self.assertRaisesRegex(ValueError, '^research_response_invalid$'):
                await research.scrape_page('https://example.com')
        with self.assertRaisesRegex(ValueError, '^research_response_invalid$'):
            research._normalize_exa_results({'results': [{'url': 'https://example.com', 'text': chr(0xd800)}]})

    async def test_requested_reported_url_provenance_not_silently_relabelled(self):
        payload = {'data': {'markdown': 'source text', 'metadata': {'sourceURL': 'https://reported.example/source'}}}
        with patch.dict('os.environ', {'FIRECRAWL_API_KEY': 'synthetic'}), patch.object(research, '_provider_json', return_value=payload):
            result = await research.scrape_page('https://requested.example/source')
        self.assertEqual('https://requested.example/source', result['url'])
        self.assertEqual(result['url'], result['requested_url'])
        self.assertEqual('https://reported.example/source', result['reported_url'])
        self.assertFalse(result['source_url_matches_request'])

    def test_firecrawl_published_time_compatibility(self):
        result = research._normalize_firecrawl_results({'data': [{'url': 'https://example.com', 'metadata': {'publishedTime': '2026-11-25'}}]})
        self.assertEqual('2026-11-25', result[0]['published_date'])

    def test_mixed_safe_unsafe_search_response_fails_whole_result(self):
        for normalize, key in ((research._normalize_exa_results, 'results'), (research._normalize_firecrawl_results, 'data')):
            with self.subTest(provider=key), self.assertRaises(ValueError):
                normalize({key: [{'url': 'https://example.com', 'text': 'safe'}, {'url': 'http://127.0.0.1/private'}]})

    async def test_surrogate_metadata_and_wire_json_fail_stably(self):
        for field in ('title', 'publishedDate', 'url'):
            item = {'url': 'https://example.com', 'text': 'safe', field: chr(0xd800)}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, '^research_response_invalid$'):
                research._normalize_exa_results({'results': [item]})
        payload = {'data': {'markdown': 'safe', 'metadata': {'title': chr(0xd800)}}}
        with patch.dict('os.environ', {'FIRECRAWL_API_KEY': 'synthetic'}), patch.object(research, '_provider_json', return_value=payload):
            with self.assertRaisesRegex(ValueError, '^research_response_invalid$'):
                await research.scrape_page('https://example.com')
        with self.assertRaisesRegex(ValueError, '^research_response_invalid$'):
            await self.request(Chunks([json.dumps({'value': chr(0xd800)}).encode()]))
