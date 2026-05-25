"""Tests for ``_extract_json`` — tolerance to LLM-produced JSON shapes.

Surfaced 2026-04-28 in an end-to-end test: a QC LLM emitted a
fenced JSON block surrounded by prose narration. The bare-parse
attempt failed on the surrounding text; the fenced regex matched
something unexpected; result was a ValueError that crashed QC
review.

The failure pattern is provider-agnostic — any model can emit
prose-wrapped JSON, multi-block responses, JSON-with-trailing-think,
or other variations. Modulatio is provider-agnostic, so the parser
must be too.

The fixed extractor walks three strategies in order: fenced block →
bare body → balanced-brace scan. The third catches everything
reasonable a model could emit.
"""
from __future__ import annotations

import pytest

from modulatio.orchestration import _extract_json


def test_extract_clean_bare_json():
    """The simplest case: response is JUST a JSON object."""
    src = '{"passed": true, "check": "ok"}'
    result = _extract_json(src)
    assert result == {"passed": True, "check": "ok"}


def test_extract_fenced_json_block():
    """Model wraps the verdict in a ```json``` block — common pattern."""
    src = '```json\n{"passed": true}\n```'
    result = _extract_json(src)
    assert result == {"passed": True}


def test_extract_fenced_block_no_lang_tag():
    """Plain ``` fence with no ``json`` tag still works."""
    src = '```\n{"passed": true}\n```'
    result = _extract_json(src)
    assert result == {"passed": True}


def test_extract_json_with_leading_prose():
    """Prose-wrapped JSON (the STR failure mode): 'Here is my verdict:
    {...}'. Bare-parse fails, fence isn't there, brace-scan succeeds."""
    src = (
        "Based on the import probe, I cannot verify pytest is "
        "available in this environment. My verdict:\n\n"
        '{"passed": false, "check": "pytest unavailable", '
        '"defect_type": "substantive"}'
    )
    result = _extract_json(src)
    assert result["passed"] is False
    assert result["defect_type"] == "substantive"


def test_extract_json_with_trailing_prose():
    """JSON appears at the start, prose after."""
    src = (
        '{"passed": true, "check": "ok"}\n\n'
        "I am confident in this verdict because the import probe "
        "succeeded and the function signature matches."
    )
    result = _extract_json(src)
    assert result == {"passed": True, "check": "ok"}


def test_extract_json_array():
    """List of tasks (Coordinator's emission shape) — top-level array."""
    src = (
        "Here are the tasks:\n\n"
        '[{"description": "Draft 1"}, {"description": "Draft 2"}]'
    )
    result = _extract_json(src)
    assert isinstance(result, list)
    assert len(result) == 2


def test_extract_json_with_nested_braces():
    """Brace-counting must respect nesting. ``check`` has its own {}
    — outer scan must not stop at the first ``}``."""
    src = (
        "My analysis:\n"
        '{"passed": true, "evidence": {"probes": ["import", "help"]}, '
        '"defect_type": null}\n'
        "End of verdict."
    )
    result = _extract_json(src)
    assert result["passed"] is True
    assert result["evidence"]["probes"] == ["import", "help"]


def test_extract_json_ignores_braces_inside_strings():
    """A ``}`` inside a string literal must not close the outer object."""
    src = '{"check": "value with } in it", "passed": true}'
    result = _extract_json(src)
    assert result["check"] == "value with } in it"
    assert result["passed"] is True


def test_extract_json_handles_escaped_quotes_in_strings():
    """``\\"`` inside a string mustn't end string-mode prematurely."""
    src = r'{"check": "he said \"hi\"", "passed": true}'
    result = _extract_json(src)
    assert result["check"] == 'he said "hi"'


def test_extract_json_picks_first_valid_object():
    """When a body has both a fenced JSON AND raw prose, the fenced
    block wins (it's the first attempt). Test that the resolution
    order is correct."""
    src = (
        '```json\n{"v": "fenced"}\n```\n\n'
        '{"v": "bare"}'
    )
    result = _extract_json(src)
    assert result["v"] == "fenced"


def test_extract_json_falls_through_when_fenced_is_malformed():
    """If the fenced block's content is invalid JSON, fall back to
    bare-body / brace-scan instead of raising."""
    src = (
        "```json\nthis is not json\n```\n\n"
        '{"v": "bare"}'
    )
    result = _extract_json(src)
    assert result["v"] == "bare"


def test_extract_json_raises_when_no_balanced_brace():
    """No JSON anywhere → clear error message."""
    src = "Just prose. No JSON here. The model went off-script."
    with pytest.raises(ValueError, match="could not parse"):
        _extract_json(src)


def test_extract_json_strips_thinking_block():
    """``<think>`` blocks are removed before extraction (existing
    behavior). Verify the new helper keeps it."""
    src = (
        "<think>let me verify this passes</think>\n"
        '{"passed": true}'
    )
    result = _extract_json(src)
    assert result == {"passed": True}


def test_extract_json_picks_first_when_multiple_blocks():
    """Some models emit multiple fenced JSON blocks (e.g., one for
    'verdict' and one for 'evidence'). The extractor takes the FIRST
    block and ignores the rest — caller's contract is one verdict
    per response."""
    src = (
        '```json\n{"v": "first"}\n```\n\n'
        "Additional context:\n\n"
        '```json\n{"v": "second"}\n```'
    )
    result = _extract_json(src)
    assert result == {"v": "first"}


def test_extract_json_handles_json_inside_markdown_header():
    """Models sometimes prefix JSON with a markdown header. The
    leading ``## Verdict`` is just prose; the JSON below should
    extract cleanly via the brace-scan fallback."""
    src = (
        "## Verdict\n\n"
        '{"passed": true, "check": "header-prefixed"}'
    )
    result = _extract_json(src)
    assert result["passed"] is True
    assert result["check"] == "header-prefixed"


def test_extract_json_with_trailing_thinking_block():
    """Reasoning model leaks ``<think>`` AFTER the JSON. Existing
    ``_strip_thinking`` removes it before extraction. Verify that
    integration is intact."""
    src = (
        '{"passed": true}\n\n'
        "<think>\nlet me double-check by re-reading the artifact\n</think>"
    )
    result = _extract_json(src)
    assert result == {"passed": True}


def test_extract_json_array_with_nested_objects():
    """Coordinator emission shape: list of task dicts, each with
    its own evidence array. Brace-counting must walk the whole
    structure."""
    src = (
        "Here are the tasks:\n\n"
        '[\n'
        '  {"description": "Draft 1", "evidence_required": [{"kind": "artifact"}]},\n'
        '  {"description": "Draft 2", "evidence_required": [{"kind": "metric"}]}\n'
        ']'
    )
    result = _extract_json(src)
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["evidence_required"][0]["kind"] == "artifact"


def test_extract_json_handles_unicode_in_strings():
    """The em-dash + smart quotes that broke our prose-trim work also
    need to round-trip through JSON cleanly. Real models emit them."""
    src = '{"check": "scope is — wide", "passed": true}'
    result = _extract_json(src)
    assert result["check"] == "scope is — wide"


def test_extract_json_handles_newlines_inside_strings():
    """JSON-encoded newline (``\\n``) inside a string value must not
    confuse the brace counter. Multi-line `notes` field is common."""
    src = '{"notes": "line one\\nline two\\nline three", "passed": false}'
    result = _extract_json(src)
    assert result["notes"] == "line one\nline two\nline three"


def test_extract_json_falls_through_when_first_brace_fails_to_parse():
    """Brace-scan must advance past a non-parseable opener. Worst
    case: multiple ``{`` occurrences where the first doesn't parse
    (e.g. it's part of prose) but a later one does."""
    src = (
        "The format is roughly { key: value } but our actual verdict is:\n\n"
        '{"passed": true}'
    )
    # The first `{` (in prose, no quotes) won't parse; brace-scan
    # advances past it to the real JSON.
    result = _extract_json(src)
    assert result == {"passed": True}


def test_extract_json_handles_narrate_then_emit_qc_shape():
    """The exact STR-T-002 failure shape: the QC LLM narrated a
    failed pytest probe, then emitted a fenced JSON block at the
    end. Reproduced from a real run; the test stays provider-agnostic
    so it remains valid regardless of which model fills the QC slot."""
    src = (
        "I attempted to verify the test file by running\n"
        "`python3 -c 'import pytest'`. The probe failed with\n"
        "ModuleNotFoundError. Without pytest available, I cannot run\n"
        "the tests, but I CAN verify the file's syntax and structure.\n\n"
        "The test file:\n"
        "- Has the correct three test functions (positive/negative/zero)\n"
        "- Uses appropriate assertions\n"
        "- Imports add from the correct module\n\n"
        "```json\n"
        '{"passed": true, "check": "structural review only — pytest unavailable", '
        '"defect_type": null, "notes": "tests appear correct but were not executed"}\n'
        "```"
    )
    result = _extract_json(src)
    assert result["passed"] is True
    assert "pytest unavailable" in result["check"]
