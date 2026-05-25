"""Tests for the artifact code-fence stripping fix.

Surfaced 2026-04-28 in the live TUI test where the drafter produced a
working Python script but wrapped it in triple-backtick markdown
fences. The orchestrator saved the raw response verbatim to
``crypto_price_logger.py``, breaking the script with a SyntaxError on
the very first line.

The fix: ``_strip_code_fences`` removes the leading ``triple-backtick
<lang>\\n`` and trailing ``\\n triple-backtick`` wrappers from the
response before write. Defensive — the prompt also tells the drafter
not to wrap in fences, but LLMs disobey sometimes.
"""
from __future__ import annotations


from modulatio.orchestration import _strip_code_fences


def test_strip_python_fence():
    """The classic case from the WLT-followup TUI test."""
    src = (
        "```python\n"
        "#!/usr/bin/env python3\n"
        "print('hello')\n"
        "```"
    )
    out = _strip_code_fences(src)
    assert out.startswith("#!/usr/bin/env python3")
    assert "```" not in out


def test_strip_bare_fence():
    """Generic ```\\n...\\n``` (no language tag) — also stripped."""
    src = "```\nplain content here\n```"
    out = _strip_code_fences(src)
    assert out == "plain content here"


def test_strip_javascript_fence():
    """Other languages get the same treatment."""
    src = "```javascript\nconsole.log('hi');\n```"
    out = _strip_code_fences(src)
    assert "```" not in out
    assert out.startswith("console.log")


def test_strip_with_leading_whitespace():
    """The drafter sometimes leads with a blank line before the fence."""
    src = "\n\n```python\ncontent\n```\n"
    out = _strip_code_fences(src)
    assert out.startswith("content") or out == "content"
    assert "```" not in out


def test_no_fence_passes_through_unchanged():
    """Content without fences is preserved exactly — don't break
    Markdown documents whose body contains literal fences as content."""
    src = "# Real markdown\n\nA paragraph.\n"
    assert _strip_code_fences(src) == src


def test_inline_fence_preserved():
    """Mid-text ``` is not a wrapper — leave it alone. Only top/tail
    fences indicate the LLM wrapped the entire artifact."""
    src = "Some prose\n```python\ncode\n```\nMore prose after\n"
    out = _strip_code_fences(src)
    # Inner fence stays — that's a legitimate code block inside markdown.
    assert "```python" in out
    assert "More prose after" in out


def test_strip_only_when_fence_pair_wraps_whole_body():
    """If only an opening fence is present (no closing), don't strip —
    that's malformed input and stripping would lose context."""
    src = "```python\nstart of code without close fence"
    # Ambiguous case — leave it alone OR strip just the opener.
    # Implementation: strip the opener anyway, since the .py file was
    # the failing case and we'd rather error on missing close than
    # have a fence in line 1.
    out = _strip_code_fences(src)
    assert not out.startswith("```")


def test_idempotent():
    """Running the strip twice gives the same result."""
    src = "```python\nprint('hi')\n```"
    once = _strip_code_fences(src)
    twice = _strip_code_fences(once)
    assert once == twice


# ── Prose-dominated code extraction (post-CDE end-to-end test) ─────────────
#
# Failure mode surfaced in the CDE end-to-end real-model test:
# GLM-5.1 produced output like "I see — the passive profile is
# restricted. Let me produce: ```code```. Actually let me also do:
# ```code```" — an em-dash on line 1 broke `python3 -c 'import file'`
# and crashed QC's verification probe. The strip-fence pipeline
# preserved the prose verbatim (no top/tail wrap), so the .py file
# was non-Python. Fix: when artifact_kind hints code, extract the
# largest fenced block from prose-dominated responses.


def test_is_code_artifact_kind():
    from modulatio.orchestration import _is_code_artifact_kind

    # Positive cases.
    for k in ("code", "python", "python_module", "python_script",
              "test_module", "py_test", "javascript_test", "ts"):
        assert _is_code_artifact_kind(k), f"expected {k!r} → True"

    # Negative cases.
    for k in (None, "", "essay", "article", "report", "advice", "research"):
        assert not _is_code_artifact_kind(k), f"expected {k!r} → False"


def test_extract_code_from_prose_extracts_largest_fenced_block():
    """The canonical CDE failure: prose intro + fenced code + more
    prose + larger fenced code. Extract the largest fenced block."""
    from modulatio.orchestration import _extract_code_from_prose

    src = (
        "I see — the passive profile is restricted. Let me produce "
        "the artifact directly. Here is a first draft:\n\n"
        "```python\ndef add(a, b):\n    return a + b\n```\n\n"
        "However, the signature should include a docstring. Let me "
        "produce the complete module:\n\n"
        '```python\n"""add.py — addition utility."""\n\n'
        "def add(a, b: int) -> int:\n"
        '    """Return the sum of a and b."""\n'
        "    return a + b\n```\n"
    )
    out = _extract_code_from_prose(src)
    assert out is not None
    assert "addition utility" in out  # the larger block won
    # Prose-level em-dash ("I see —") is gone; an em-dash INSIDE a
    # Python docstring is fine (valid Unicode in source). Verify the
    # specific prose phrase is gone, not the byte itself.
    assert "I see" not in out
    assert "first draft" not in out


def test_extract_code_from_prose_returns_none_when_no_fences():
    """Pure prose with no fenced blocks → None. The caller falls back
    to the unmodified body."""
    from modulatio.orchestration import _extract_code_from_prose

    src = "This is just prose. No code at all.\nAnother line."
    assert _extract_code_from_prose(src) is None


def test_extract_code_from_prose_returns_none_when_minimal_prose():
    """When the response is mostly-already-code (only 1–2 prose lines
    above a fence), don't extract — the fence wrapping was probably
    the model's whole body and the existing _strip_code_fences will
    handle it."""
    from modulatio.orchestration import _extract_code_from_prose

    src = "```python\ndef f(): return 1\n```"
    assert _extract_code_from_prose(src) is None

    src2 = "Sure!\n```python\ndef f(): return 1\n```"
    # Only 1 prose line — below threshold.
    assert _extract_code_from_prose(src2) is None


def test_extract_code_from_prose_handles_mixed_languages():
    """If multiple fences with different language tags exist, take the
    largest by content size regardless of language tag."""
    from modulatio.orchestration import _extract_code_from_prose

    src = (
        "Here is a small JS sample:\n\n"
        "```javascript\nlet x = 1;\n```\n\n"
        "And a longer Python module — note the difference in scope:\n\n"
        "```python\n"
        + "\n".join(f"def fn_{i}(): return {i}" for i in range(20))
        + "\n```\n"
    )
    out = _extract_code_from_prose(src)
    assert out is not None
    assert "fn_0" in out
    assert "let x" not in out  # the JS block was smaller, not picked


def test_extract_code_handles_unfenced_prose_with_dash():
    """The actual CDE failure-mode signature: 'I see —' on line 1.
    The em-dash is the breaking character; without prose-extraction
    it survives into the .py file."""
    from modulatio.orchestration import _extract_code_from_prose

    src = (
        "I see — let me produce the artifact:\n\n"
        "```python\nprint('clean')\n```\n\n"
        "Note that this is straightforward.\n"
        "Another reasoning line.\n"
    )
    out = _extract_code_from_prose(src)
    assert out is not None
    assert "—" not in out
    assert out.strip() == "print('clean')"


# ── Trim leading prose before unfenced code (post-STR e2e test) ───────────
#
# Surfaced in the STR run: GLM-5.1's engineer emitted "I can't use
# redirection in passive mode. Let me just emit the code directly:" +
# blank + #!/usr/bin/env python3 + correct module body. NO fences
# wrap the body. _extract_code_from_prose returns None (correctly —
# requires fenced blocks). The new step finds the first code-shaped
# line and chops everything before it.


def test_trim_leading_prose_strips_above_shebang():
    from modulatio.orchestration import _trim_leading_prose_from_code

    src = (
        "I can't use redirection in passive mode. Let me just emit "
        "the code directly as the artifact:\n\n"
        "#!/usr/bin/env python3\n"
        '"""Module providing addition."""\n\n'
        "def add(a, b):\n"
        "    return a + b\n"
    )
    out = _trim_leading_prose_from_code(src)
    assert out.startswith("#!/usr/bin/env python3")
    assert "I can't use" not in out


def test_trim_leading_prose_strips_above_import():
    from modulatio.orchestration import _trim_leading_prose_from_code

    src = (
        "I've exhausted my safe probes. Producing the test file now:\n\n"
        "import pytest\n"
        "from add import add\n\n"
        "def test_add(): assert add(1, 2) == 3\n"
    )
    out = _trim_leading_prose_from_code(src)
    assert out.startswith("import pytest")
    assert "exhausted" not in out


def test_trim_leading_prose_strips_above_def():
    from modulatio.orchestration import _trim_leading_prose_from_code

    src = (
        "Sure! Here is the function:\n\n"
        "def add(a, b):\n"
        "    return a + b\n"
    )
    out = _trim_leading_prose_from_code(src)
    assert out.startswith("def add")


def test_trim_leading_prose_strips_above_docstring():
    from modulatio.orchestration import _trim_leading_prose_from_code

    src = (
        "Let me produce the module:\n\n"
        '"""Hello world module."""\n\n'
        "def main(): print('hi')\n"
    )
    out = _trim_leading_prose_from_code(src)
    assert out.startswith('"""Hello world module."""')


def test_trim_leading_prose_strips_above_comment():
    """Comment-only first line is valid Python — should be preserved
    as code, no trim applied."""
    from modulatio.orchestration import _trim_leading_prose_from_code

    src = (
        "# Author note: produces add operation\n"
        "def add(a, b): return a + b\n"
    )
    out = _trim_leading_prose_from_code(src)
    assert out == src  # unchanged — line 1 is already code-shaped


def test_trim_leading_prose_no_change_when_starts_with_code():
    from modulatio.orchestration import _trim_leading_prose_from_code

    src = (
        "import json\n"
        "\n"
        "def f():\n"
        "    return json.dumps({'k': 'v'})\n"
    )
    out = _trim_leading_prose_from_code(src)
    assert out == src


def test_trim_leading_prose_handles_javascript():
    from modulatio.orchestration import _trim_leading_prose_from_code

    src = (
        "Producing the JS module:\n\n"
        "const add = (a, b) => a + b;\n"
        "export default add;\n"
    )
    out = _trim_leading_prose_from_code(src)
    assert out.startswith("const add")


def test_trim_leading_prose_returns_unchanged_when_no_code_marker():
    """All-prose input has no code marker → leave alone (callsite gate
    keeps this from running on essay artifacts anyway)."""
    from modulatio.orchestration import _trim_leading_prose_from_code

    src = "This is a paragraph.\nNo code anywhere here.\n"
    out = _trim_leading_prose_from_code(src)
    assert out == src
