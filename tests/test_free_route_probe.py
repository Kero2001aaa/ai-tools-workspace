import io
import json
import unittest
import urllib.error
from agent.free_route_probe import probe_free_route, ENDPOINT


class Response(io.BytesIO):
    pass


class FreeRouteProbeTests(unittest.TestCase):
    def test_exact_zero_price_request_and_success(self):
        seen = []
        def open_(req, timeout):
            seen.append(req)
            self.assertEqual(45, timeout)
            return Response(json.dumps({'choices': [{'message': {'content': 'WORKSPACE_OK'}}], 'usage': {'cost': 0}}).encode())
        result = probe_free_route('test-key-not-real', opener=open_)
        self.assertTrue(result['ok'])
        self.assertEqual(1, len(seen))
        req = seen[0]
        self.assertEqual(ENDPOINT, req.full_url)
        body = json.loads(req.data)
        self.assertEqual('openrouter/free', body['model'])
        self.assertNotIn('models', body)
        self.assertEqual({'prompt': 0, 'completion': 0}, body['provider']['max_price'])
        self.assertFalse(body['provider']['allow_fallbacks'])
        self.assertEqual('deny', body['provider']['data_collection'])
        self.assertEqual(24, body['max_tokens'])

    def test_429_no_retry_and_no_provider_body_or_key(self):
        calls = []
        def open_(req, timeout):
            calls.append(req)
            raise urllib.error.HTTPError(ENDPOINT, 429, 'private-key-leak', {}, io.BytesIO(b'private-provider-data'))
        result = probe_free_route('test-key-not-real', opener=open_)
        self.assertEqual('rate_limited', result['error'])
        self.assertFalse(result['retry'])
        self.assertEqual(1, len(calls))
        self.assertNotIn('private', json.dumps(result))
        self.assertNotIn('test-key', json.dumps(result))

    def test_cost_proof_required(self):
        for cost in [None, 1, -1, True, '0']:
            def open_(req, timeout):
                return Response(json.dumps({'choices': [{'message': {'content': 'WORKSPACE_OK'}}], 'usage': {'cost': cost}}).encode())
            self.assertEqual('zero_cost_unverified', probe_free_route('x', opener=open_)['error'])

    def test_bad_answer_is_not_success(self):
        def open_(req, timeout):
            return Response(json.dumps({'choices': [{'message': {'content': 'other'}}], 'usage': {'cost': 0}}).encode())
        self.assertFalse(probe_free_route('x', opener=open_)['ok'])

    def test_response_cap(self):
        self.assertEqual('response_too_large', probe_free_route('x', opener=lambda *a, **k: Response(b'x'*65537))['error'])

    def test_invalid_json_and_schema(self):
        for value in [b'bad', b'[]', b'{}', b'{"choices":[]}']:
            result = probe_free_route('x', opener=lambda *a, **k: Response(value))
            self.assertFalse(result['ok'])

    def test_connection_failure(self):
        def open_(req, timeout):
            raise urllib.error.URLError('private')
        self.assertEqual('connection_failed', probe_free_route('x', opener=open_)['error'])

    def test_invalid_key_before_network(self):
        for key in [None, '', ' ', 'x'*4097]:
            with self.assertRaises(ValueError):
                probe_free_route(key, opener=lambda *a, **k: self.fail('network'))
