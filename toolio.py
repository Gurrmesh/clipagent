"""Read Claude's tool calls defensively.

Claude answers through tool calls, whose arguments are meant to arrive as real
JSON: {"clips": [{...}, {...}]}. Now and then a nested array comes back as a
*string* holding the JSON instead — sometimes with a trailing comma that makes
it invalid JSON too:

    {"clips": "[\\n{\\n\\"id\\": 0, ... \\"why\\": \\"...\\",\\n},\\n..."}

Iterating that string yields characters, and the first `item.get("id")` took
a whole run down with "'str' object has no attribute 'get'". Everything that
reads a tool reply goes through here: strings that hold JSON are parsed
(leniently), and only real objects are handed on.
"""
from __future__ import annotations

import ast
import json
import re
from typing import Any, Dict, List

_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def _loads(text: str) -> Any:
    t = text.strip()
    for attempt in (t, _TRAILING_COMMA.sub(r"\1", t)):
        try:
            return json.loads(attempt)
        except ValueError:
            pass
    try:                                      # last resort: Python literal syntax
        py = _TRAILING_COMMA.sub(r"\1", t)
        py = re.sub(r"\btrue\b", "True", re.sub(r"\bfalse\b", "False", re.sub(r"\bnull\b", "None", py)))
        return ast.literal_eval(py)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None


def coerce(value: Any, depth: int = 0) -> Any:
    """Turn JSON-in-a-string back into objects, all the way down."""
    if depth > 8:
        return value
    if isinstance(value, str):
        t = value.strip()
        if len(t) >= 2 and t[0] in "[{" and t[-1] in "]}":
            parsed = _loads(t)
            if parsed is not None and not isinstance(parsed, str):
                return coerce(parsed, depth + 1)
        return value
    if isinstance(value, dict):
        return {k: coerce(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [coerce(v, depth + 1) for v in value]
    return value


def tool_inputs(message: Any) -> List[Dict[str, Any]]:
    """The (repaired) arguments of every tool call in a reply."""
    out = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", "") != "tool_use":
            continue
        inp = coerce(getattr(block, "input", None))
        if isinstance(inp, dict):
            out.append(inp)
    return out


def items(message: Any, key: str = "clips") -> List[Dict[str, Any]]:
    """The list under `key` across a reply's tool calls — objects only."""
    found: List[Dict[str, Any]] = []
    for inp in tool_inputs(message):
        value = inp.get(key)
        if isinstance(value, dict):           # one object where a list was asked for
            value = [value]
        if isinstance(value, list):
            found.extend(v for v in value if isinstance(v, dict))
    return found


def as_dict(value: Any) -> Dict[str, Any]:
    value = coerce(value)
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> List[Any]:
    value = coerce(value)
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [v for v in re.split(r"[,\s]+", value.strip()) if v]
    return []


def ask(client: Any, key: str = "clips", retries: int = 1, **kwargs) -> List[Dict[str, Any]]:
    """Call Claude and return the parsed list under `key`. A reply that holds
    nothing usable (unparseable, or empty when it should not be) is asked for
    once more. API errors are raised to the caller as before."""
    got: List[Dict[str, Any]] = []
    for _ in range(retries + 1):
        message = client.messages.create(**kwargs)
        got = items(message, key)
        if got or getattr(message, "stop_reason", "") == "max_tokens":
            break
    return got
