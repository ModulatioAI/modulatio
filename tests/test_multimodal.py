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
from modulatio.multimodal import _longest_backtick_run, _render_user_text


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


def _loaded(path, tmp_cfg=None):
    """A loaded attachment for ``path`` — the one type the block accepts, so a
    test exercises the same byte authority production does."""
    from modulatio import attachments, config

    if tmp_cfg is not None:
        config.CONFIG_DIR = tmp_cfg
    return attachments.build_attachment(path, kind="image")


def test_build_image_block_png(tmp_path: Path):
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\nfake-png-bytes")
    block = build_image_content_block(_loaded(p))
    assert block["type"] == "image_url"
    url = block["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


def test_build_image_block_jpeg(tmp_path: Path):
    p = tmp_path / "photo.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    block = build_image_content_block(_loaded(p))
    assert "image/jpeg" in block["image_url"]["url"]


def test_build_image_block_handles_jpeg_alt_extension(tmp_path: Path):
    """`.jpeg` and `.jpg` both map to image/jpeg."""
    p = tmp_path / "photo.jpeg"
    p.write_bytes(b"\xff\xd8\xff\xe0")
    assert "image/jpeg" in build_image_content_block(_loaded(p))["image_url"]["url"]


def test_build_image_block_unknown_extension_falls_back(tmp_path: Path):
    """Unknown extensions default to a generic image type so we still
    pass the bytes through rather than failing."""
    p = tmp_path / "img.xyz"
    p.write_bytes(b"raw-bytes")
    block = build_image_content_block(_loaded(p))
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
        build_image_content_block(_loaded(p))


def test_build_image_content_block_accepts_under_cap(tmp_path, monkeypatch):
    """Under-cap images encode normally — the gate doesn't change
    behavior for legitimate dispatches."""
    from modulatio.multimodal import build_image_content_block

    monkeypatch.setenv("MODULATIO_MAX_ATTACHMENT_BYTES", "100000")
    p = tmp_path / "small.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    block = build_image_content_block(_loaded(p))
    assert block["type"] == "image_url"
    assert block["image_url"]["url"].startswith("data:image/png;base64,")


# ═══ fold: test_multimodal_r2_audit.py ═══
# r2 audit regression — _render_user_text fence escaping.
#
# A document whose own content contains a triple-backtick fence must not be
# able to break out of the wrapper fence and bleed into instruction context.
# The wrapper computes a backtick run longer than any run in the content.


def _doc(tmp_path: Path, name: str, body: str):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return build_attachment(p, kind="document")


def test_longest_backtick_run_counts_consecutive():
    assert _longest_backtick_run("no backticks here") == 0
    assert _longest_backtick_run("a `b` c") == 1
    assert _longest_backtick_run("```fenced```") == 3
    assert _longest_backtick_run("text ```` four ` one") == 4


def test_document_inner_fence_cannot_break_out(tmp_path: Path):
    body = "intro\n```python\nprint('hi')\n```\noutro"
    att = _doc(tmp_path, "snippet.md", body)

    text = _render_user_text(message="please review", attachments=[att])

    # The document content survives verbatim, fences and all.
    assert body in text
    # The wrapper fence is strictly longer than the doc's own ``` run (3),
    # so the inner fences cannot terminate the wrapper.
    assert "````" in text  # four-backtick wrapper
    # The wrapper opens and closes with the same longer fence, and the
    # message lands outside any code fence.
    lines = text.splitlines()
    # The wrapper fences are the all-backtick lines longer than the inner
    # fences (>=4); they appear exactly twice (open + close).
    wrapper_lines = [ln for ln in lines if set(ln) == {"`"} and len(ln) >= 4]
    assert len(wrapper_lines) == 2, wrapper_lines
    assert wrapper_lines[0] == wrapper_lines[1]
    assert "please review" in text


def test_plain_document_still_uses_triple_fence(tmp_path: Path):
    att = _doc(tmp_path, "plain.txt", "just some prose, no fences")
    text = _render_user_text(message="hi", attachments=[att])
    assert "```" in text
    # No accidental over-fencing when content has no backticks.
    assert "````" not in text
    assert "just some prose, no fences" in text


def test_a_swapped_source_cannot_change_what_the_image_request_sends(
        tmp_path, monkeypatch):
    """Encoding from the original name sends whatever it points at NOW. The
    file can be replaced, or repointed at something outside the root the load
    was authorized against, between authorization and dispatch — and the
    digest describing the request would no longer describe what was sent."""
    import base64

    from modulatio import attachments, config
    from modulatio.multimodal import build_image_content_block

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    safe = tmp_path / "shot.png"
    safe.write_bytes(b"\x89PNG\r\n\x1a\n" + b"SAFE" * 4)
    item = attachments.build_attachment(safe, kind="image")

    # The source is replaced after the load authorized it.
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"\x89PNG\r\n\x1a\n" + b"LEAK" * 4)
    safe.unlink()
    safe.symlink_to(secret)

    url = build_image_content_block(item)["image_url"]["url"]
    sent = base64.b64decode(url.split(",", 1)[1])
    assert b"SAFE" in sent
    assert b"LEAK" not in sent, "the replacement reached the provider request"


def test_an_image_with_no_snapshot_is_refused_rather_than_fetched(tmp_path):
    """A snapshot is what makes the bytes vouchable. Without one there is
    nothing to send but a re-read of a path whose contents are no longer the
    ones that were loaded."""
    import pytest

    from modulatio.attachments import Attachment
    from modulatio.multimodal import build_image_content_block

    orphan = Attachment(kind="image", path=tmp_path / "gone.png",
                        name="gone.png", content=None)
    with pytest.raises(ValueError, match="no engine-held snapshot"):
        build_image_content_block(orphan)


def test_bytes_that_no_longer_match_their_digest_are_refused(
        tmp_path, monkeypatch):
    """The digest is the claim the request makes about itself. Sending bytes
    it does not describe would make the record of what was sent a fiction."""
    import pytest

    from modulatio import attachments, config
    from modulatio.multimodal import build_image_content_block

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    src = tmp_path / "shot.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"REAL" * 4)
    item = attachments.build_attachment(src, kind="image")

    # Tamper with the engine's own copy.
    item.staged_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"HACK" * 4)
    with pytest.raises(ValueError, match="does not match the digest"):
        build_image_content_block(item)
