# SPDX-License-Identifier: Apache-2.0
"""Round-3 re-sweep regressions for telegram_notify.

Finding 1: ``_split_chunks`` measured Python code points against the 4096
cap, but Telegram counts UTF-16 code units. An emoji-heavy chunk could be
under the code-point cap yet ~2x over the wire cap -> HTTP 400. The fix
budgets by UTF-16 code units everywhere, including the hard-split path.
"""

from modulatio import telegram_notify as tn

# A non-BMP emoji: 1 Python code point, 2 UTF-16 code units.
EMOJI = "\U0001F600"  # grinning face


def _utf16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def test_utf16_len_counts_surrogate_pairs():
    assert tn._utf16_len("a") == 1
    assert tn._utf16_len(EMOJI) == 2
    assert tn._utf16_len(EMOJI * 5) == 10


def test_emoji_heavy_single_line_chunks_respect_utf16_cap():
    # 4000 emoji = 4000 code points (passes a naive len() <= 4000 check) but
    # 8000 UTF-16 units. Without the fix this returns one oversized chunk.
    text = EMOJI * 4000
    chunks = tn._split_chunks(text, max_len=tn._MAX_MESSAGE_LENGTH)
    assert chunks, "expected at least one chunk"
    for chunk in chunks:
        assert _utf16(chunk) <= tn._MAX_MESSAGE_LENGTH, (
            f"chunk of {_utf16(chunk)} UTF-16 units exceeds cap "
            f"{tn._MAX_MESSAGE_LENGTH}"
        )
    # Lossless: reassembling the pieces reproduces the input.
    assert "".join(chunks) == text


def test_emoji_heavy_multiline_chunks_respect_utf16_cap():
    # Many emoji-only lines whose code-point total is under 4000 but whose
    # UTF-16 total is over it.
    text = "\n".join(EMOJI * 100 for _ in range(40))  # 40 lines, 4000+ units
    chunks = tn._split_chunks(text)
    for chunk in chunks:
        assert _utf16(chunk) <= tn._MAX_MESSAGE_LENGTH
    assert "".join(chunks) == text


def test_hard_split_never_breaks_a_surrogate_pair():
    # Every chunk must re-decode cleanly (no lone surrogate / mojibake) and
    # round-trip through utf-16. A naive code-point slice could cut here.
    text = EMOJI * 5000
    chunks = tn._split_chunks(text)
    for chunk in chunks:
        # Re-encoding must succeed and the emoji count must stay whole.
        assert chunk.encode("utf-16-le").decode("utf-16-le") == chunk
        assert len(chunk) == chunk.count(EMOJI)


def test_ascii_behaviour_unchanged():
    # Short ASCII -> single chunk; the BMP fast path is preserved.
    assert tn._split_chunks("hello world") == ["hello world"]
    # An ASCII message just over the cap splits and round-trips.
    text = "x" * (tn._MAX_MESSAGE_LENGTH + 50)
    chunks = tn._split_chunks(text)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert _utf16(chunk) <= tn._MAX_MESSAGE_LENGTH
    assert "".join(chunks) == text


def test_line_boundary_split_preserved_for_ascii():
    # Multi-line ASCII still splits on line boundaries (keepends preserves \n).
    line = "a" * 1000 + "\n"
    text = line * 10  # ~10010 units, forces multiple chunks
    chunks = tn._split_chunks(text)
    for chunk in chunks:
        assert _utf16(chunk) <= tn._MAX_MESSAGE_LENGTH
    assert "".join(chunks) == text
