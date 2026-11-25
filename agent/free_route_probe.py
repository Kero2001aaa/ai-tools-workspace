"""One explicit zero-price OpenRouter diagnostic. Never enabled automatically.

Uses a generic fixed prompt. Does not accept project/user content, arbitrary URLs,
paid models, fallback models, or browser credentials. No retry on any failure.
"""
from __future__ import annotations
import json
import time
import urllib.error
import urllib.request

ENDPOINT = 'https://openrouter.ai/api/v1/chat/completions'
MODEL = 'openrouter/free'
MAX_RESPONSE_BYTES = 65536


def probe_free_route(api_key: str, *, opener=None, clock=time.monotonic) -> dict:
    if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 4096:
        raise ValueError('A configured API key is required')
    body = {
        'model': MODEL,
        'messages': [{'role': 'user', 'content': 'Reply with exactly WORKSPACE_OK. No tools or external actions.'}],
        'max_tokens': 24,
        'provider': {
            'max_price': {'prompt': 0, 'completion': 0},
            'data_collection': 'deny',
            'allow_fallbacks': False,
        },
    }
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(body).encode(), method='POST',
        headers={'Authorization': 'Bearer ' + api_key.strip(), 'Content-Type': 'application/json'},
    )
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    start = clock()
    try:
        with (opener or urllib.request.build_opener(NoRedirect).open)(req, timeout=45) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return {'ok': False, 'error': 'response_too_large', 'retry': False}
        result = json.loads(raw)
        answer = result.get('choices', [{}])[0].get('message', {}).get('content')
        cost = result.get('usage', {}).get('cost')
        # Do not treat missing cost evidence as verified zero cost.
        if cost is None or isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost != 0:
            return {'ok': False, 'error': 'zero_cost_unverified', 'retry': False}
        return {
            'ok': answer == 'WORKSPACE_OK',
            'error': None if answer == 'WORKSPACE_OK' else 'unexpected_answer',
            'cost': cost, 'retry': False,
            'elapsed_seconds': round(clock() - start, 3),
        }
    except urllib.error.HTTPError as exc:
        # Never return provider body, headers, key or private exception text.
        category = {401: 'auth_failed', 402: 'quota_or_billing_blocked', 429: 'rate_limited'}.get(exc.code, 'provider_http_error')
        return {'ok': False, 'error': category, 'http_status': exc.code, 'retry': False}
    except (urllib.error.URLError, TimeoutError):
        return {'ok': False, 'error': 'connection_failed', 'retry': False}
    except (ValueError, TypeError, AttributeError, IndexError):
        return {'ok': False, 'error': 'invalid_response', 'retry': False}
