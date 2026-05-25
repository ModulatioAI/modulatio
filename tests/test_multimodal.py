"""Tests for #31c.2 — multimodal chat dispatch.

When a chat message has at least one image attachment, ``chat_with_agent``
routes through the multimodal path: builds a LiteLLM-format ``messages``
list with image content blocks (base64 data URIs) and calls
``litellm.completion`` directly.

Text-only chats and document-only chats stay on the existing
single-prompt path.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from modulatio.attachments import build_attachment
from modulatio.chat import chat_with_agent
from modulatio.multimodal import (
    MIME_TYPES,
    build_image_content_block,
    build_multimodal_messages,
)
from modulatio.roster import Agent


def _make_agent(**overrides) -> Agent:
    base = dict(
        id="leader", name="Leader",
        identity="You are the Leader.",
        skills=["leader"], model="test/vision-capable-model",
        tier="leader",
    )
    base.update(overrides)
    return Agent(**base)


# ─── Image encoding ─────────────────────────────────────────────────────────


def test_build_image_block_png(tmp_path: Path):
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\nfake-png-bytes")
    block = build_image_content_block(p)
    assert block["type"] == "image_url"
    url = block["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


def test_build_image_block_jpeg(tmp_path: Path):
    p = tmp_path / "photo.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    block = build_image_content_block(p)
    assert "image/jpeg" in block["image_url"]["url"]


def test_build_image_block_handles_jpeg_alt_extension(tmp_path: Path):
    """`.jpeg` and `.jpg` both map to image/jpeg."""
    p = tmp_path / "photo.jpeg"
    p.write_bytes(b"\xff\xd8\xff\xe0")
    assert "image/jpeg" in build_image_content_block(p)["image_url"]["url"]


def test_build_image_block_unknown_extension_falls_back(tmp_path: Path):
    """Unknown extensions default to a generic image type so we still
    pass the bytes through rather than failing."""
    p = tmp_path / "img.xyz"
    p.write_bytes(b"raw-bytes")
    block = build_image_content_block(p)
    assert block["image_url"]["url"].startswith("data:image/")


def test_mime_types_covers_common_formats():
    """Sanity check the table covers the formats vision models accept."""
    assert MIME_TYPES[".png"] == "image/png"
    assert MIME_TYPES[".jpg"] == "image/jpeg"
    assert MIME_TYPES[".jpeg"] == "image/jpeg"
    assert MIME_TYPES[".gif"] == "image/gif"
    assert MIME_TYPES[".webp"] == "image/webp"


# ─── Messages list construction ─────────────────────────────────────────────


def test_messages_has_system_role_with_identity(tmp_path: Path):
    p = tmp_path / "x.png"
    p.write_bytes(b"png")
    img = build_attachment(p, kind="image")
    messages = build_multimodal_messages(
        agent=_make_agent(identity="You are strict and skeptical."),
        message="what's in this image?",
        history=[],
        attachments=[img],
    )
    sys_msg = messages[0]
    assert sys_msg["role"] == "system"
    assert "strict and skeptical" in sys_msg["content"]


def test_messages_user_content_includes_text_and_image(tmp_path: Path):
    p = tmp_path / "x.png"
    p.write_bytes(b"png")
    messages = build_multimodal_messages(
        agent=_make_agent(),
        message="describe this",
        history=[],
        attachments=[build_attachment(p, kind="image")],
    )
    user_msg = messages[-1]
    assert user_msg["role"] == "user"
    # User content is a list of blocks: text + image_url.
    assert isinstance(user_msg["content"], list)
    types = [block["type"] for block in user_msg["content"]]
    assert "text" in types
    assert "image_url" in types
    text_block = next(b for b in user_msg["content"] if b["type"] == "text")
    assert "describe this" in text_block["text"]


def test_messages_inlines_text_documents_into_user_text(tmp_path: Path):
    """Documents are quoted in the text portion of the user message
    (not as separate content blocks) so the LLM treats them as
    pre-context, not unique items."""
    img = tmp_path / "x.png"
    img.write_bytes(b"png")
    doc = tmp_path / "spec.md"
    doc.write_text("BUDGET: under 5K\n")
    messages = build_multimodal_messages(
        agent=_make_agent(),
        message="implement this",
        history=[],
        attachments=[
            build_attachment(doc, kind="document"),
            build_attachment(img, kind="image"),
        ],
    )
    text_block = next(b for b in messages[-1]["content"] if b["type"] == "text")
    assert "BUDGET: under 5K" in text_block["text"]


def test_messages_includes_history_as_separate_turns(tmp_path: Path):
    """Prior conversation turns become real user/assistant messages
    in the list — not stitched into a single user prompt — so the
    model sees the conversation structurally."""
    img = tmp_path / "x.png"
    img.write_bytes(b"png")
    messages = build_multimodal_messages(
        agent=_make_agent(),
        message="follow up",
        history=[
            ("user", "first question"),
            ("assistant", "first answer"),
        ],
        attachments=[build_attachment(img, kind="image")],
    )
    roles = [m["role"] for m in messages]
    assert roles == ["system", "user", "assistant", "user"]
    # Prior turns are plain string content (no images attached then).
    assert messages[1]["content"] == "first question"
    assert messages[2]["content"] == "first answer"


# ─── chat_with_agent routing ────────────────────────────────────────────────


def test_chat_routes_to_multimodal_when_image_attached(tmp_path: Path):
    """An image attachment triggers the multimodal path: chat_with_agent
    calls into the chat_completion injected for tests, not the
    single-shot runner_factory."""
    img = tmp_path / "x.png"
    img.write_bytes(b"png")

    # Stub the LiteLLM-style completion fn.
    captured: list = []
    def fake_completion(*, model, messages, **kwargs):
        captured.append({"model": model, "messages": messages})
        m = MagicMock()
        m.choices = [MagicMock()]
        m.choices[0].message.content = "vision-response"
        return m

    runner_called = []
    def runner_factory(model: str):
        runner_called.append(model)
        return lambda p: "should-not-be-called"

    out = chat_with_agent(
        agent=_make_agent(),
        message="what's in this?",
        history=[],
        runner_factory=runner_factory,
        attachments=[build_attachment(img, kind="image")],
        chat_completion=fake_completion,
    )

    assert out == "vision-response"
    assert len(captured) == 1
    assert captured[0]["model"] == "test/vision-capable-model"
    assert runner_called == [], "single-shot runner should not be used for vision"


def test_chat_uses_single_prompt_when_no_image(tmp_path: Path):
    """Document-only or text-only chat keeps using the runner_factory
    single-prompt path — multimodal is only for image attachments."""
    doc = tmp_path / "x.md"
    doc.write_text("doc content")

    runner_called = []
    def runner_factory(model: str):
        def runner(prompt: str) -> str:
            runner_called.append(prompt)
            return "doc-response"
        return runner

    completion_called = []
    def fake_completion(**kwargs):
        completion_called.append(kwargs)
        return None

    out = chat_with_agent(
        agent=_make_agent(),
        message="summarize",
        history=[],
        runner_factory=runner_factory,
        attachments=[build_attachment(doc, kind="document")],
        chat_completion=fake_completion,
    )

    assert out == "doc-response"
    assert len(runner_called) == 1
    assert completion_called == [], "completion not called for non-image chats"


# === image size cap defense-in-depth =====


def test_build_image_content_block_rejects_oversize(tmp_path, monkeypatch):
    """Defense-in-depth: callers that hand-construct Attachment instances
    and skip ``build_attachment`` still hit the cap when the image is
    actually base64-encoded for dispatch."""
    from modulatio.multimodal import build_image_content_block

    monkeypatch.setenv("MODULATIO_MAX_ATTACHMENT_BYTES", "1024")
    p = tmp_path / "big.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096)
    with pytest.raises(ValueError, match="exceeds the image cap"):
        build_image_content_block(p)


def test_build_image_content_block_accepts_under_cap(tmp_path, monkeypatch):
    """Under-cap images encode normally — the gate doesn't change
    behavior for legitimate dispatches."""
    from modulatio.multimodal import build_image_content_block

    monkeypatch.setenv("MODULATIO_MAX_ATTACHMENT_BYTES", "100000")
    p = tmp_path / "small.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    block = build_image_content_block(p)
    assert block["type"] == "image_url"
    assert block["image_url"]["url"].startswith("data:image/png;base64,")
