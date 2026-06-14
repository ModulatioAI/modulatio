"""Regression tests for the r2 debug findings on delivery._looks_binary.

Findings:
  - resource-leak: _looks_binary leaked a file descriptor (open() not closed).
  - edge-case: a fixed-size 8192-byte read can split a multibyte UTF-8 code
    point at the boundary, mis-classifying valid text as binary.
"""

from __future__ import annotations

import gc
import warnings
from pathlib import Path

from modulatio.delivery import _looks_binary


def test_looks_binary_does_not_leak_file_descriptor(tmp_path: Path) -> None:
    """The read must happen inside a context manager so no fd is left open
    (and no ResourceWarning is emitted for an unclosed file)."""
    p = tmp_path / "plain.md"
    p.write_text("# hello\nsome text\n", encoding="utf-8")

    gc.collect()
    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        # Repeated calls would accumulate leaked fds before the fix; under the
        # ResourceWarning-as-error filter an unclosed file surfaces here.
        for _ in range(50):
            assert _looks_binary(p) is False
        gc.collect()


def test_looks_binary_utf8_split_at_boundary_is_text(tmp_path: Path) -> None:
    """A valid UTF-8 file whose 8192-byte head ends mid-multibyte-char must
    NOT be classified as binary."""
    # Fill so that the 4-byte emoji straddles the 8192-byte read boundary.
    emoji = "\U0001f600"  # 4 UTF-8 bytes
    encoded_emoji = emoji.encode("utf-8")
    assert len(encoded_emoji) == 4
    # Pad with ASCII so the emoji begins at byte 8190 -> only 2 of its 4 bytes
    # land inside the 8192-byte head read.
    padding = "a" * 8190
    content = (padding + emoji + "more valid text after").encode("utf-8")
    p = tmp_path / "wide.md"
    p.write_bytes(content)

    # Sanity: the raw head genuinely fails a naive strict decode (proving the
    # boundary truncation is real).
    head = content[:8192]
    naive_failed = False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        naive_failed = True
    assert naive_failed, "test setup must straddle the boundary"

    assert _looks_binary(p) is False


def test_looks_binary_real_binary_still_detected(tmp_path: Path) -> None:
    """Genuine binary content (NUL byte and/or invalid UTF-8 mid-stream) is
    still classified as binary."""
    nul = tmp_path / "nul.bin"
    nul.write_bytes(b"PK\x03\x04\x00\x00 rest of a zip")
    assert _looks_binary(nul) is True

    invalid = tmp_path / "invalid.bin"
    # Invalid UTF-8 in the MIDDLE of the head (not a truncated tail).
    invalid.write_bytes(b"text \xff\xfe more text" + b"x" * 100)
    assert _looks_binary(invalid) is True


def test_looks_binary_plain_text_false(tmp_path: Path) -> None:
    p = tmp_path / "plain.txt"
    p.write_text("just normal prose, nothing binary here\n", encoding="utf-8")
    assert _looks_binary(p) is False


def test_looks_binary_missing_file_false(tmp_path: Path) -> None:
    assert _looks_binary(tmp_path / "nope.md") is False
