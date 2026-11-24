"""Fail-closed HTTP boundary for the one verified zero-price chat route.

Every request is inspected after SDK/runtime overrides, before network I/O.
No generic host, model list, tools, paid fallback, or redirect is accepted.
"""
import json
import httpx

URL = 'https://openrouter.ai/api/v1/chat/completions'
PROVIDER = {'max_price': {'prompt': 0, 'completion': 0}, 'data_collection': 'deny', 'allow_fallbacks': False}


def validate_request(request):
    if request.method != 'POST' or str(request.url) != URL:
        raise ValueError('Free-only route endpoint rejected')
    try:
        body = json.loads(request.content)
    except (ValueError, TypeError):
        raise ValueError('Free-only route body rejected') from None
    if not isinstance(body, dict) or body.get('model') != 'openrouter/free':
        raise ValueError('Free-only model required')
    if any(k in body for k in ['models', 'route', 'plugins', 'tools', 'tool_choice']):
        raise ValueError('Unverified routing or tool extension rejected')
    if body.get('provider') != PROVIDER:
        raise ValueError('Zero-price privacy policy required')
    if body.get('stream') is not False:
        raise ValueError('Streaming is not yet verified')
    cap = body.get('max_tokens', body.get('max_completion_tokens'))
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 512:
        raise ValueError('Bounded output required')


class FreeRouteTransport(httpx.BaseTransport):
    def __init__(self, inner=None):
        self.inner = inner or httpx.HTTPTransport(retries=0)

    def handle_request(self, request):
        validate_request(request)
        return self.inner.handle_request(request)

    def close(self):
        self.inner.close()


class AsyncFreeRouteTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner=None):
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        validate_request(request)
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()
