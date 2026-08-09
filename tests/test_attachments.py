"""Tests for #31c — chat attachments (data model + chat dispatch).

The pane stores Attachment objects in its per-pane attach list. On send,
they're passed to ``chat_with_agent`` which formats them into the prompt:

  - Text-readable documents: file content is quoted inline so the LLM
    sees what's in them.
  - Images: included as a path reference. True multimodal vision
    (base64 / image_url content blocks) is a future micro-slice — this
    lays the groundwork without changing the runner contract.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio.attachments import build_attachment


def test_build_attachment_reads_text_document(tmp_path: Path):
    """A .txt / .md / .py file is read as utf-8 and content stored on
    the Attachment so chat dispatch can quote it inline."""
    p = tmp_path / "notes.md"
    p.write_text("# Notes\n\nTwo bullets:\n- one\n- two\n", encoding="utf-8")
    att = build_attachment(p, kind="document")
    assert att.kind == "document"
    assert att.path == p
    assert att.name == "notes.md"
    assert att.content is not None
    assert "Two bullets" in att.content


def test_build_attachment_image_has_no_content(tmp_path: Path):
    """Images don't get utf-8-decoded — content stays None and path
    travels through to the prompt as a reference. True multimodal will
    re-read these as bytes / base64 in a later slice."""
    p = tmp_path / "diagram.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    att = build_attachment(p, kind="image")
    assert att.kind == "image"
    assert att.content is None
    assert att.path.suffix == ".png"


def test_build_attachment_raises_when_path_missing(tmp_path: Path):
    p = tmp_path / "does-not-exist.md"
    with pytest.raises(FileNotFoundError):
        build_attachment(p, kind="document")


def test_build_attachment_raises_on_non_utf8_document(tmp_path: Path):
    """A binary document with no extractor fails fast rather than dispatch a
    garbled prompt. A PDF is the exception: it routes to text extraction, and
    a malformed one gets the extractor's own named refusal."""
    p = tmp_path / "binary.docx"
    p.write_bytes(b"PK\x03\x04\x00\x01\x02\x03not-utf-8-binary\xff\xfe")
    with pytest.raises(UnicodeDecodeError):
        build_attachment(p, kind="document")


# === size caps ===========================


def test_build_attachment_rejects_oversize_document(tmp_path, monkeypatch):
    """a misclick on a multi-MB log
    file would have spiked memory and dispatched an oversized payload
    to the LLM. Now `build_attachment` rejects above the cap BEFORE
    reading bytes off disk."""
    monkeypatch.setenv("MODULATIO_MAX_ATTACHMENT_BYTES", "1024")  # 1 KiB
    p = tmp_path / "big.txt"
    p.write_text("x" * 4096, encoding="utf-8")  # 4 KiB > 1 KiB cap
    with pytest.raises(ValueError, match="exceeds the document cap"):
        build_attachment(p, kind="document")


def test_build_attachment_rejects_oversize_image(tmp_path, monkeypatch):
    """Same gate for images. Oversized images would have base64-encoded
    into a multi-MB content block (≈33% larger again than the raw bytes)
    and broken context windows."""
    monkeypatch.setenv("MODULATIO_MAX_ATTACHMENT_BYTES", "1024")
    p = tmp_path / "big.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096)
    with pytest.raises(ValueError, match="exceeds the image cap"):
        build_attachment(p, kind="image")


def test_build_attachment_accepts_under_cap(tmp_path, monkeypatch):
    """Files under the cap are unaffected — the gate is a defense, not
    a change of behavior for normal-sized inputs."""
    monkeypatch.setenv("MODULATIO_MAX_ATTACHMENT_BYTES", "1024")
    p = tmp_path / "small.md"
    p.write_text("hello", encoding="utf-8")
    att = build_attachment(p, kind="document")
    assert att.content == "hello"


def test_build_attachment_default_caps_pinned(monkeypatch):
    """Pin the default caps so a future regression that quietly raises
    or lowers them gets caught."""
    from modulatio import attachments
    monkeypatch.delenv("MODULATIO_MAX_ATTACHMENT_BYTES", raising=False)
    assert attachments.DEFAULT_MAX_DOCUMENT_BYTES == 1 * 1024 * 1024
    assert attachments.DEFAULT_MAX_IMAGE_BYTES == 10 * 1024 * 1024


def test_build_attachment_respects_env_override(tmp_path, monkeypatch):
    """The env override wins over the default. Power users / debugging
    tasks can lift the cap when they genuinely need to."""
    monkeypatch.setenv("MODULATIO_MAX_ATTACHMENT_BYTES", "5")
    p = tmp_path / "over.md"
    p.write_text("hello world", encoding="utf-8")  # 11 bytes > 5
    with pytest.raises(ValueError, match="exceeds"):
        build_attachment(p, kind="document")


def test_build_attachment_malformed_env_falls_back_to_default(tmp_path, monkeypatch):
    """A non-int env value must NOT crash the dispatch — fall back
    silently to the default. The env path is the last-mile escape
    hatch for debugging; bad values get treated as 'no override'."""
    monkeypatch.setenv("MODULATIO_MAX_ATTACHMENT_BYTES", "not-an-int")
    p = tmp_path / "small.md"
    p.write_text("hello", encoding="utf-8")
    att = build_attachment(p, kind="document")
    assert att.content == "hello"


def test_replacing_the_source_after_loading_changes_nothing(tmp_path, monkeypatch):
    """What was loaded is what gets used. A source replaced, truncated or
    grown after the fact would otherwise change the request already made."""
    from modulatio import attachments, config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    src = tmp_path / "note.md"
    src.write_text("original bytes\n")

    att = attachments.build_attachment(src, kind="document")
    assert att.staged_path is not None
    assert oct(att.staged_path.stat().st_mode & 0o777) == "0o600"
    assert att.sha256.startswith("sha256:")

    src.write_text("swapped entirely\n")
    assert att.staged_path.read_text() == "original bytes\n"


def test_a_symlink_loads_its_target_through_the_one_open(tmp_path, monkeypatch):
    """Following the link is safe under one-open semantics: whatever the open
    resolves to IS what gets checked, capped, digested and staged — there is
    no second look for a repointed name to diverge from, and the staged copy
    makes any later repointing irrelevant."""
    from modulatio import attachments, config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    target = tmp_path / "real.md"
    target.write_text("the target body\n")
    link = tmp_path / "link.md"
    link.symlink_to(target)

    item = attachments.build_attachment(link, kind="document")
    assert item.content == "the target body\n"

    # Repointing the name afterwards changes nothing already loaded.
    target2 = tmp_path / "other.md"
    target2.write_text("different\n")
    link.unlink()
    link.symlink_to(target2)
    assert item.staged_path.read_text() == "the target body\n"


def test_a_fifo_is_refused_without_blocking_on_it(tmp_path, monkeypatch):
    """A plain read-open of a FIFO waits forever for a writer, so the refusal
    must come from a descriptor the open actually returned — non-blocking
    open, then the regular-file check."""
    import os as _os

    import pytest

    from modulatio import attachments, config

    if not hasattr(_os, "mkfifo"):
        pytest.skip("mkfifo unavailable")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    fifo = tmp_path / "pipe.md"
    _os.mkfifo(fifo)

    with pytest.raises(ValueError, match="not a regular file"):
        attachments.build_attachment(fifo, kind="document")


def test_the_text_comes_from_the_snapshot_not_a_second_read_of_the_path(
        tmp_path, monkeypatch):
    """Reading the path for content and the descriptor for the digest asks the
    same name twice, and the answers can disagree — the text dispatched is then
    not the bytes that were measured. Proven by making a path read impossible:
    the load still succeeds, so nothing reached for the path again."""
    from pathlib import Path as _Path

    from modulatio import attachments, config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    src = tmp_path / "notes.md"
    src.write_text("the real body\n")

    def _refuse(*a, **k):
        raise AssertionError("the path was read a second time")

    monkeypatch.setattr(_Path, "read_text", _refuse)
    item = attachments.build_attachment(src, kind="document")

    assert item.content == "the real body\n"
    assert item.size == len("the real body\n")
    assert item.sha256.startswith("sha256:")


def test_a_file_that_grows_past_the_cap_while_copying_is_refused(tmp_path, monkeypatch):
    """A cap checked against a size read beforehand bounds nothing: the copy
    that follows is a separate act on a file that may have grown, and a source
    whose reported size is meaningless could smuggle bytes past the limit."""
    import pytest

    from modulatio import attachments, config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setenv(attachments._OVERRIDE_ENV, "64")
    big = tmp_path / "big.md"
    big.write_bytes(b"x" * 4096)

    with pytest.raises(ValueError) as caught:
        attachments.build_attachment(big, kind="document")
    assert "cap" in str(caught.value)
    # The partial copy does not outlive the refusal.
    staged = tmp_path / "cfg" / "loaded"
    assert not [p for p in staged.glob("*") if p.is_file()]


def test_an_image_does_not_put_a_host_path_in_the_prompt(tmp_path, monkeypatch):
    """The operator's filesystem layout is not part of the request: a host
    path discloses where they keep things and names a file the model may then
    try to reach by other means."""
    from modulatio import attachments, chat, config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    img = tmp_path / "diagram.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    att = attachments.build_attachment(img, kind="image")

    from modulatio.roster import Agent

    prompt = chat._build_prompt(
        agent=Agent(id="leader", name="Leader", role="leader", model="stub"),
        message="look at this", history=[], attachments=[att])
    assert "diagram.png" in prompt
    assert str(tmp_path) not in prompt
    assert str(img) not in prompt


def test_a_pdf_loads_through_its_text_layer(tmp_path, monkeypatch):
    """A PDF document is extracted by the same contained helper a file read
    uses — one parser for both paths — so the ingestion pipe carries its text
    instead of refusing the bytes as undecodable."""
    import pytest as _pytest

    from modulatio import attachments, config, tools

    if tools.shutil.which("pdftotext") is None:
        _pytest.skip("poppler-utils not installed")
    from tests.test_tools import _tiny_pdf

    # Extraction of untrusted input happens confined or not at all, so the
    # suite-wide bypass is lifted for this one.
    from modulatio import sandbox
    monkeypatch.delenv("MODULATIO_RUN_SHELL_UNSAFE", raising=False)
    monkeypatch.setenv("MODULATIO_SANDBOX_PROFILE", "standard")
    sandbox.reset_enforcement_state_cache()
    if sandbox.enforcement_state() is not sandbox.EnforcementState.SANDBOXED_FULL:
        _pytest.skip("host cannot seal the sandbox")

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    src = tmp_path / "paper.pdf"
    src.write_bytes(_tiny_pdf())

    item = attachments.build_attachment(src, kind="document")
    assert "the owl flies at midnight" in (item.content or "")
    assert item.sha256.startswith("sha256:")



def test_a_snapshot_does_not_outlive_a_failed_construction(tmp_path, monkeypatch):
    """When construction fails after the copy is taken, no caller ever
    received the path and nobody is left to release it. A snapshot with no
    owner is not evidence of anything — it is a file nothing comes back for."""
    import pytest

    from modulatio import attachments, config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    binary = tmp_path / "sheet.xlsx"
    binary.write_bytes(b"PK\x03\x04" + bytes(range(256)) * 4)

    with pytest.raises(UnicodeDecodeError):
        attachments.build_attachment(binary, kind="document")

    staged = tmp_path / "cfg" / "loaded"
    assert not [p for p in staged.glob("*") if p.is_file()]


def test_snapshots_nobody_claimed_are_swept_by_age(tmp_path, monkeypatch):
    """A turn that died between staging and dispatch leaves a copy nobody
    comes back for, and a staging directory that only grows is a pile of the
    operator's documents kept for no reason. Age alone decides, so an
    attachment in flight — staged moments ago — cannot be reached."""
    import os as _os
    import time as _time

    from modulatio import attachments, config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    src = tmp_path / "live.md"
    src.write_text("in flight\n")
    live = attachments.build_attachment(src, kind="document")

    stale = (tmp_path / "cfg" / "loaded") / "abandoned"
    stale.write_text("nobody's\n")
    old = _time.time() - attachments._ORPHAN_TTL_S - 60
    _os.utime(stale, (old, old))

    assert attachments.sweep_orphan_snapshots() == 1
    assert not stale.exists()
    assert live.staged_path.exists(), "an in-flight snapshot was swept"


def test_bytes_must_come_from_the_folder_that_was_authorized(
        tmp_path, monkeypatch):
    """Resolving a name under an allowed root and then opening that name are
    two acts, and the file can be repointed between them. The single open makes
    the bytes internally consistent; only checking the DESCRIPTOR proves they
    came from the root the grant covered."""
    import pytest

    from modulatio import attachments, config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    granted = tmp_path / "granted"
    granted.mkdir()
    outside = tmp_path / "elsewhere" / "secret.md"
    outside.parent.mkdir()
    outside.write_text("NOT UNDER THE GRANT\n")

    inside = granted / "notes.md"
    inside.symlink_to(outside)

    with pytest.raises(ValueError, match="outside the folder it was authorized"):
        attachments.build_attachment(inside, kind="document", within=(granted,))

    # A real file under the grant still loads, and so does a link that stays
    # inside it — the check is about where the bytes came from, not about links.
    real = granted / "real.md"
    real.write_text("under the grant\n")
    assert attachments.build_attachment(
        real, kind="document", within=(granted,)).content == "under the grant\n"

    hop = granted / "hop.md"
    hop.symlink_to(real)
    assert attachments.build_attachment(
        hop, kind="document", within=(granted,)).content == "under the grant\n"
