"""Pre-V2 Slice B tests — project design-intent module.

Covers: file path resolution, body parsing (with + without
frontmatter), prompt rendering with both authored and missing
intent, defensive failure paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import design_intent, vault


@pytest.fixture
def isolated_vault(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    vault.init_project("tst", "Test", "obj")
    return tmp_path


def _write_intent(project_code: str, body: str, *, with_frontmatter: bool = True):
    """Helper: write a design-intent file under <project>/standards/."""
    p = design_intent.design_intent_path(project_code)
    p.parent.mkdir(parents=True, exist_ok=True)
    if with_frontmatter:
        p.write_text(
            "---\n"
            "type: design-intent\n"
            "version: 1\n"
            "---\n"
            f"\n{body}\n"
        )
    else:
        p.write_text(body)


def test_design_intent_path_uses_standards_subdir(isolated_vault):
    """File lives at <project>/standards/design-intent.md — same dir
    as artifact-kind standards, distinguished by frontmatter type."""
    p = design_intent.design_intent_path("tst")
    assert p.name == "design-intent.md"
    assert p.parent.name == "standards"


def test_load_body_returns_empty_when_file_missing(isolated_vault):
    """No file authored → empty string. Producers receive the neutral
    marker and execute without project-specific constraints."""
    assert design_intent.load_body("tst") == ""


def test_load_body_strips_frontmatter(isolated_vault):
    """Frontmatter is metadata; body is what reaches the prompt. The
    parser drops the frontmatter cleanly."""
    _write_intent("tst", "- **Decision:** Markdown vault, not SQLite.")
    body = design_intent.load_body("tst")
    assert "type: design-intent" not in body
    assert "Markdown vault" in body


def test_load_body_handles_no_frontmatter_lenient(isolated_vault):
    """A user who hand-authors the file without bothering with
    frontmatter still gets their content reaching producers — the
    whole file is treated as body. Frontmatter is recommended for
    machine-parseability but not required."""
    _write_intent(
        "tst", "- **Decision:** Python stdlib only.",
        with_frontmatter=False,
    )
    body = design_intent.load_body("tst")
    assert "Python stdlib only" in body


def test_render_for_prompt_includes_authored_body(isolated_vault):
    """Rendered slot wraps the body with the canonical section header
    + delimiters that producer / Coordinator / Leader / QC prompts
    consume. Single source of truth for the format."""
    _write_intent(
        "tst",
        "- **Decision:** Model-agnostic.\n  **Reason:** "
        "Avoid lock-in to any single provider.",
    )
    rendered = design_intent.render_for_prompt("tst")
    assert "DESIGN INTENT" in rendered
    assert "Model-agnostic" in rendered
    assert "BEGIN DESIGN INTENT" in rendered
    assert "END DESIGN INTENT" in rendered


def test_render_for_prompt_returns_neutral_marker_when_absent(isolated_vault):
    """No design-intent file → rendered slot has the canonical empty
    marker. Stable shape across populated / empty cases so prompts
    template deterministically."""
    rendered = design_intent.render_for_prompt("tst")
    assert "DESIGN INTENT" in rendered
    assert "No design-intent file authored" in rendered
    # No begin/end markers when the file isn't authored.
    assert "BEGIN DESIGN INTENT" not in rendered


def test_load_body_returns_empty_on_unreadable_file(isolated_vault, monkeypatch):
    """OSError on read → empty body, doesn't crash. Defensive: a
    transiently-locked file shouldn't block producer dispatch."""
    p = design_intent.design_intent_path("tst")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("body")

    real_read_text = Path.read_text

    def boom(self, *args, **kwargs):
        if self == p:
            raise OSError("simulated lock")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boom)
    assert design_intent.load_body("tst") == ""


def test_load_body_returns_empty_on_non_utf8_file(isolated_vault):
    """A non-UTF-8 design-intent file (UnicodeDecodeError, a ValueError
    subclass — NOT an OSError) must degrade to empty body, not crash
    render_for_prompt and the four producer/decompose/QC/Leader call
    sites. Mirrors constitution/qc_persona defensive contract."""
    p = design_intent.design_intent_path("tst")
    p.parent.mkdir(parents=True, exist_ok=True)
    # Invalid UTF-8 continuation byte → read_text(encoding="utf-8") raises
    # UnicodeDecodeError.
    p.write_bytes(b"---\ntype: design-intent\n---\n\nbroken \xff\xfe body\n")

    assert design_intent.load_body("tst") == ""
    # And the rendered prompt slot falls back to the neutral marker.
    rendered = design_intent.render_for_prompt("tst")
    assert "No design-intent file authored" in rendered
