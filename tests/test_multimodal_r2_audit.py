"""r2 audit regression — _render_user_text fence escaping.

A document whose own content contains a triple-backtick fence must not be
able to break out of the wrapper fence and bleed into instruction context.
The wrapper computes a backtick run longer than any run in the content.
"""
from __future__ import annotations

from pathlib import Path

from modulatio.attachments import build_attachment
from modulatio.multimodal import _longest_backtick_run, _render_user_text


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
