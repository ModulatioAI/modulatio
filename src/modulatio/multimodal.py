"""Multimodal chat dispatch (slice #31c.2).

When a chat message has at least one image attachment, the regular
single-prompt path can't carry the image bytes — we route to a
LiteLLM-format ``messages`` list with image content blocks (base64
data URIs) and call ``litellm.completion`` directly.

LiteLLM normalises the provider-specific shapes (Anthropic native
``image`` blocks vs OpenAI ``image_url`` vs Gemini parts vs OpenRouter
pass-through) — we always emit OpenAI-style ``image_url`` blocks and
LiteLLM translates downstream. Models that don't support vision raise
at completion time; the caller sees the error in the pane.

Document attachments stay inline-quoted in the user message's text
block (rather than getting their own content block) so the LLM treats
them as pre-context. Only images use the multimodal blocks path.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Callable

from modulatio.attachments import Attachment
from modulatio.roster import Agent

#: Common image extensions → mime type. Vision models accept these.
MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

#: Fallback mime type for unknown image extensions — generic so we still
#: pass bytes through rather than failing at the dispatch layer.
_FALLBACK_MIME = "image/octet-stream"


def build_image_content_block(path: Path) -> dict[str, Any]:
    """Build an OpenAI-format ``image_url`` content block for an image
    file. Returns ``{"type": "image_url", "image_url": {"url": "data:..."}}``.

    LiteLLM rewrites this into provider-native shapes (Anthropic
    ``image`` blocks, Gemini parts, etc.) at completion time.

    re-checks the image-size cap before
    reading the file. The attach-time check in ``build_attachment``
    is the primary gate; this is defense-in-depth for callers that
    construct Attachment instances by hand and skip the builder.
    """
    from modulatio.attachments import (
        DEFAULT_MAX_IMAGE_BYTES, _OVERRIDE_ENV, _resolve_cap,
    )

    cap = _resolve_cap(DEFAULT_MAX_IMAGE_BYTES)
    size = path.stat().st_size
    if size > cap:
        raise ValueError(
            f"image attachment {path.name!r} is {size} bytes; exceeds "
            f"the image cap of {cap} bytes "
            f"(override via {_OVERRIDE_ENV})."
        )

    mime = MIME_TYPES.get(path.suffix.lower(), _FALLBACK_MIME)
    raw = path.read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{encoded}"},
    }


def build_multimodal_messages(
    *,
    agent: Agent,
    message: str,
    history: list[tuple[str, str]],
    attachments: list[Attachment],
) -> list[dict[str, Any]]:
    """Build a LiteLLM-format messages list for multimodal chat.

    Layout:
        [
            {"role": "system",    "content": "<agent identity>"},
            {"role": "user",      "content": "<prior user turn>"},        # plain str
            {"role": "assistant", "content": "<prior assistant turn>"},   # plain str
            ... more history ...
            {"role": "user",      "content": [
                {"type": "text",      "text": "<message + inlined docs>"},
                {"type": "image_url", "image_url": {"url": "data:..."}},
                ... more images ...
            ]},
        ]

    Documents (``kind='document'``) are quoted into the text portion of
    the trailing user message — they're text context, not standalone
    items. Images get one ``image_url`` block each.
    """
    identity = (agent.identity or f"You are {agent.name}.").strip()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": identity},
    ]

    for role, content in history:
        if role not in ("user", "assistant"):
            continue
        messages.append({"role": role, "content": content})

    text = _render_user_text(message=message, attachments=attachments)
    user_content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for att in attachments:
        if att.kind == "image":
            user_content.append(build_image_content_block(att.path))
    messages.append({"role": "user", "content": user_content})
    return messages


def _render_user_text(
    *, message: str, attachments: list[Attachment],
) -> str:
    """Compose the text portion of the trailing user message: any
    document attachments quoted inline, then the new user message."""
    parts: list[str] = []
    docs = [a for a in attachments if a.kind == "document"]
    if docs:
        parts.append("# Attached documents")
        for doc in docs:
            parts.append(f"## `{doc.name}`")
            parts.append("")
            parts.append("```")
            parts.append(doc.content or "")
            parts.append("```")
        parts.append("")
    parts.append(message)
    return "\n".join(parts)


def chat_with_agent_multimodal(
    *,
    agent: Agent,
    message: str,
    history: list[tuple[str, str]],
    attachments: list[Attachment],
    chat_completion: Callable[..., Any] | None = None,
) -> str:
    """Multimodal chat dispatch — calls ``litellm.completion`` (or the
    injected ``chat_completion`` for tests) with a LiteLLM-format
    messages list and returns the response text.
    """
    if not agent.model:
        raise ValueError(
            f"agent {agent.id!r} has no model configured — "
            "multimodal chat dispatch requires an explicit model"
        )
    if chat_completion is None:
        from litellm import completion as chat_completion  # type: ignore[no-redef]

    messages = build_multimodal_messages(
        agent=agent, message=message, history=history,
        attachments=attachments,
    )
    response = chat_completion(model=agent.model, messages=messages)
    # LiteLLM mirrors the OpenAI response shape — choices[0].message.content
    # is the assistant text.
    return response.choices[0].message.content


__all__ = [
    "MIME_TYPES",
    "build_image_content_block",
    "build_multimodal_messages",
    "chat_with_agent_multimodal",
]
