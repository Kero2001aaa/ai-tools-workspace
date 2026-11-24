"""Offline candidate for bounded read-only tool calling. Not wired to runtime.

The default production transport continues to deny tools. This contract only
permits caller-pinned function schemas and text/tool messages. Price and privacy
checks are inherited from the production plain-text validator. No plugins,
images, endpoint changes or fallback can be introduced here.
"""
import copy
import json
import re
import httpx
from agent.free_route_guard import validate_request, PROVIDER

READ_ONLY_NAMES = frozenset({'workspace_status', 'recent_memories'})
TOOL_PROVIDER = {**PROVIDER, 'require_parameters': True}


class FreeToolContract:
    def __init__(self, tools):
        if not isinstance(tools, list) or not 1 <= len(tools) <= 2:
            raise ValueError('One or two pinned read-only tool schemas required')
        schemas = copy.deepcopy(tools)
        names = set()
        for schema in schemas:
            if not isinstance(schema, dict) or set(schema) != {'type', 'function'} or schema['type'] != 'function':
                raise ValueError('Only function schemas accepted')
            fn = schema['function']
            if not isinstance(fn, dict) or set(fn) - {'name', 'description', 'parameters'}:
                raise ValueError('Unsupported function schema')
            name = fn.get('name')
            if not isinstance(name, str) or name not in READ_ONLY_NAMES or name in names:
                raise ValueError('Only unique pinned read-only functions accepted')
            params = fn.get('parameters')
            if not isinstance(params, dict) or params.get('type') != 'object':
                raise ValueError('Object parameters required')
            names.add(name)
        if len(json.dumps(schemas)) > 16384:
            raise ValueError('Tool schemas too large')
        self._schemas = schemas
        self._names = frozenset(names)

    def validate(self, request):
        try:
            body = json.loads(request.content)
        except (ValueError, TypeError):
            raise ValueError('Tool body invalid') from None
        if not isinstance(body, dict) or body.get('tools') != self._schemas:
            raise ValueError('Tool schemas must exactly match pinned read-only schemas')
        if body.get('provider') != TOOL_PROVIDER:
            raise ValueError('Free private parameter-aware provider required')
        choice = body.get('tool_choice', 'auto')
        if choice not in ('auto', 'none'):
            raise ValueError('Only auto or none tool choice verified')
        if body.get('parallel_tool_calls', False) is not False:
            raise ValueError('Parallel calls are not verified')
        messages = body.get('messages')
        if not isinstance(messages, list) or not 1 <= len(messages) <= 64:
            raise ValueError('Bounded messages required')
        pending = set()
        completed = set()
        plain = []
        for msg in messages:
            if not isinstance(msg, dict):
                raise ValueError('Message invalid')
            role = msg.get('role')
            if role == 'tool':
                if set(msg) != {'role', 'content', 'tool_call_id'} or not isinstance(msg['tool_call_id'], str) or msg['tool_call_id'] not in pending or not isinstance(msg['content'], str):
                    raise ValueError('Unmatched tool result')
                pending.remove(msg['tool_call_id'])
                completed.add(msg['tool_call_id'])
            elif role == 'assistant' and 'tool_calls' in msg:
                if set(msg) - {'role', 'content', 'tool_calls'} or msg.get('content') not in (None, ''):
                    raise ValueError('Unsupported tool assistant message')
                calls = msg['tool_calls']
                if not isinstance(calls, list) or len(calls) != 1 or pending:
                    raise ValueError('Exactly one serial tool call required')
                call = calls[0]
                if not isinstance(call, dict) or set(call) != {'id', 'type', 'function'} or call['type'] != 'function':
                    raise ValueError('Invalid function call')
                cid = call['id']
                fn = call['function']
                if not isinstance(cid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', cid) or cid in completed:
                    raise ValueError('Invalid or duplicate tool call ID')
                if not isinstance(fn, dict) or set(fn) != {'name', 'arguments'} or not isinstance(fn['name'], str) or fn['name'] not in self._names:
                    raise ValueError('Unpinned function call')
                args = fn['arguments']
                if not isinstance(args, str) or len(args) > 8192:
                    raise ValueError('Tool arguments too large')
                try:
                    parsed = json.loads(args, object_pairs_hook=_unique_object)
                    if not isinstance(parsed, dict):
                        raise ValueError('Tool arguments must be object')
                    if fn['name'] == 'workspace_status' and parsed:
                        raise ValueError('Status takes no arguments')
                    if fn['name'] == 'recent_memories':
                        limit = parsed.get('limit', 10)
                        if set(parsed) - {'limit'} or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
                            raise ValueError('Memory limit must be bounded')
                except (ValueError, TypeError):
                    raise ValueError('Tool arguments invalid') from None
                pending.add(cid)
            else:
                if pending or not isinstance(role, str) or role not in {'system', 'user', 'assistant'} or set(msg) != {'role', 'content'} or not isinstance(msg['content'], str):
                    raise ValueError('Only plain text or matched tool messages accepted')
            plain.append({'role': 'user', 'content': 'contract-validated message'})
        if pending:
            raise ValueError('Pending tool call requires its result')
        # Reuse the actual production endpoint,model,price,privacy,output checks.
        check = {k:v for k,v in body.items() if k not in {'tools', 'tool_choice', 'parallel_tool_calls'}}
        check['provider'] = PROVIDER
        check['messages'] = plain
        validate_request(httpx.Request(request.method, request.url, json=check))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate argument key')
        result[key] = value
    return result
