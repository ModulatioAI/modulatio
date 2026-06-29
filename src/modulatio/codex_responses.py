# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Codex (ChatGPT-subscription) Responses-API dialect.

GPT-5.5 via the OpenAI Codex OAuth subscription is reached through the ChatGPT
backend (``chatgpt.com/backend-api/codex/responses``), which speaks the
**Responses API**, not chat-completions. This module is the pure translation
layer between Modulatio's chat-completions vocabulary (what the tool-loop speaks)
and the Responses API:

- :func:`codex_input_from_messages` — chat ``messages`` → ``(instructions, input)``
  (typed Responses input items; system → instructions).
- :func:`codex_tools_from_chat` — chat tool schema → Responses tool schema.
- :func:`chat_response_from_codex_stream` — aggregate a streamed Responses
  result into a ``ChatResponse`` (text + tool_calls).

All pure (no I/O, no litellm), so the runner stays thin and a future web surface
can reuse it. Recipe validated live 2026-06-19 (see the reference engram).
"""
from __future__ import annotations

import json
import warnings
from typing import Any

# litellm serializes the Codex Responses usage object into a model whose shape
# the ChatGPT backend doesn't quite match, emitting a noisy pydantic serializer
# UserWarning to stderr on every codex turn. Codex is a flat-rate subscription
# (we don't record its usage), so the warning is pure terminal clutter for the
# user. Silence just that one message, module-level (thread-safe, unlike
# catch_warnings()) and narrowly scoped so real warnings elsewhere still surface.
warnings.filterwarnings(
    "ignore", message="Pydantic serializer warnings", category=UserWarning
)

_DEFAULT_INSTRUCTIONS = "You are a helpful assistant."


def _text_of(content: Any) -> str:
    """Coerce a chat message ``content`` to text (str passthrough; else JSON)."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content)


def codex_input_from_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Convert chat-completions ``messages`` to ``(instructions, input_items)``.

    System messages fold into ``instructions`` (the Responses API's system slot;
    the backend REQUIRES a non-empty value). User/assistant text become
    ``message`` items; assistant ``tool_calls`` become ``function_call`` items;
    ``role="tool"`` results become ``function_call_output`` items — correlated by
    the same ``call_id`` the chat path uses, so a multi-turn tool loop threads
    correctly.
    """
    instructions_parts: list[str] = []
    items: list[dict] = []
    for m in messages or []:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            txt = _text_of(content)
            if txt:
                instructions_parts.append(txt)
            continue
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": m.get("tool_call_id") or m.get("call_id") or "",
                "output": _text_of(content),
            })
            continue
        if role == "assistant":
            if content:
                items.append({
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": _text_of(content)}],
                })
            for tc in (m.get("tool_calls") or []):
                fn = (tc.get("function") if isinstance(tc, dict) else None) or {}
                items.append({
                    "type": "function_call",
                    "call_id": (tc.get("id") if isinstance(tc, dict) else "") or "",
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments") or "{}",
                })
            continue
        # user (and any other role) → an input_text message
        items.append({
            "type": "message", "role": role or "user",
            "content": [{"type": "input_text", "text": _text_of(content)}],
        })
    instructions = "\n\n".join(p for p in instructions_parts if p) or _DEFAULT_INSTRUCTIONS
    return instructions, items


def codex_tools_from_chat(tools: list[dict] | None) -> list[dict]:
    """Flatten chat-completions tool schema (``{type:function, function:{…}}``)
    into the Responses shape (``{type:function, name, description, parameters}``).
    An already-Responses-shaped tool passes through."""
    out: list[dict] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function")
        if isinstance(fn, dict):
            out.append({
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            })
        elif t.get("type") == "function" and t.get("name"):
            out.append(t)  # already Responses-shaped
    return out


def chat_response_from_codex_stream(stream: Any, *, timeout: "float | None" = None) -> "Any":
    """Aggregate a streamed Codex Responses result into a ``ChatResponse``.

    Collects ``output_text`` deltas into the content, and ``function_call`` items
    (name + call_id from the item events, arguments accumulated from the
    ``function_call_arguments.delta`` events, falling back to the item's final
    ``arguments``). Event types are matched on their string form so a litellm
    enum-rename can't silently break parsing.

    ``timeout`` (Op B): a wall-clock deadline on the stream-consume loop. The
    per-call watchdog bounds request INITIATION, not stream ITERATION — a server
    that trickles keepalives forever would otherwise spin this loop indefinitely at
    high CPU, uncatchable by the watchdog and unkillable (worker thread). With a
    deadline a wedged stream raises ``TimeoutError`` so the call fails into normal
    recovery instead of hanging the run. ``None`` keeps the prior unbounded behavior.
    """
    import time as _time

    from modulatio.runners import ChatResponse, ToolCall

    deadline = _time.monotonic() + timeout if timeout else None
    text_parts: list[str] = []
    calls: dict[str, dict] = {}
    order: list[str] = []

    def _ensure(iid: str) -> dict:
        if iid not in calls:
            calls[iid] = {"name": None, "call_id": None, "args": ""}
            order.append(iid)
        return calls[iid]

    for ev in stream:
        if deadline is not None and _time.monotonic() > deadline:
            raise TimeoutError(
                f"Codex stream exceeded {timeout}s without a terminal event"
            )
        t = str(getattr(ev, "type", "")).lower()
        if "output_text" in t and "delta" in t:
            text_parts.append(getattr(ev, "delta", "") or "")
        elif "function_call_arguments" in t and "delta" in t:
            rec = _ensure(getattr(ev, "item_id", None) or "_fc")
            rec["args"] += getattr(ev, "delta", "") or ""
        elif "output_item" in t and ("added" in t or "done" in t):
            item = getattr(ev, "item", None)
            if item is not None and getattr(item, "type", None) == "function_call":
                rec = _ensure(getattr(item, "id", None) or "_fc")
                rec["name"] = getattr(item, "name", None) or rec["name"]
                rec["call_id"] = getattr(item, "call_id", None) or rec["call_id"]
                ia = getattr(item, "arguments", None)
                if ia and not rec["args"]:
                    rec["args"] = ia

    tool_calls = []
    for iid in order:
        rec = calls[iid]
        if not rec["name"]:
            continue
        try:
            args = json.loads(rec["args"]) if rec["args"] else {}
        except (ValueError, TypeError):
            args = {}
        tool_calls.append(ToolCall(id=rec["call_id"] or iid, name=rec["name"], args=args))

    return ChatResponse(content="".join(text_parts), tool_calls=tuple(tool_calls))


__all__ = [
    "codex_input_from_messages",
    "codex_tools_from_chat",
    "chat_response_from_codex_stream",
]
