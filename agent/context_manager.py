"""Conservative, dependency-free conversation context budgeting.

This is a heuristic history selector, not semantic prompt compression. It keeps
complete user-turn groups so assistant tool calls and their tool results are
not separated. The newest turn is always retained, even if it exceeds budget.
"""
from __future__ import annotations

import math
from typing import Any, Iterable


def _message_text(message: Any) -> str:
    """Serialize message content and tool-call metadata for rough token sizing."""
    parts = [str(getattr(message, "content", ""))]
    additional = getattr(message, "additional_kwargs", None)
    if additional:
        parts.append(str(additional))
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        parts.append(str(tool_calls))
    return "\n".join(parts)


def estimate_message_tokens(message: Any) -> int:
    """Estimate tokens conservatively from characters plus per-message overhead."""
    return 4 + math.ceil(len(_message_text(message)) / 3.5)


def estimate_tokens(messages: Iterable[Any]) -> int:
    """Estimate total message tokens; this is not a provider tokenizer."""
    return sum(estimate_message_tokens(message) for message in messages)


def _turn_groups(messages: list[Any]) -> list[list[Any]]:
    """Group each HumanMessage with everything up to the next HumanMessage."""
    groups: list[list[Any]] = []
    current: list[Any] = []
    for message in messages:
        if getattr(message, "type", "") == "human" and current:
            groups.append(current)
            current = []
        current.append(message)
    if current:
        groups.append(current)
    return groups


def compact_messages(messages: Iterable[Any], max_tokens: int) -> list[Any]:
    """Keep the newest whole turns that fit; never split tool-call sequences.

    The newest turn is always retained, even when it alone exceeds max_tokens.
    Messages are returned in their original chronological order.
    """
    if max_tokens < 1:
        raise ValueError("max_tokens must be at least 1.")

    original = list(messages)
    if not original or estimate_tokens(original) <= max_tokens:
        return original

    groups = _turn_groups(original)
    kept_reversed: list[list[Any]] = []
    used = 0
    for group in reversed(groups):
        group_cost = estimate_tokens(group)
        if not kept_reversed:
            # Preserve the active turn even if the request itself is oversized.
            kept_reversed.append(group)
            used += group_cost
            continue
        if used + group_cost > max_tokens:
            break
        kept_reversed.append(group)
        used += group_cost

    kept = [message for group in reversed(kept_reversed) for message in group]
    return kept
