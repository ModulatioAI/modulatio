"""Tests for mechanical assembly (assembly.py) — the manifest parser +
disk-concatenation that replaced LLM re-emission for consolidation.

The bug this fixes: a consolidation producer that re-typed N unit bodies as
output tokens truncated at the model's output cap (6 stories → 2). Now it
emits a small manifest and the engine concatenates unit files from disk.
These tests cover the parser's strictness, the concatenation order/framing,
and — the security-sensitive part — the path gate on producer-named units.
"""

from __future__ import annotations

from pathlib import Path

from modulatio import assembly


# ── manifest parsing ──────────────────────────────────────────────────────


def _fence(body: str) -> str:
    return f"prose before\n\n```assembly\n{body}\n```\n\n## summary_for_state_doc\nx"


def test_parses_valid_manifest():
    m = assembly.parse_assembly_manifest(
        _fence('{"units": ["a.txt", "b.txt"], "title_page": "T", "separator": "|"}')
    )
    assert m is not None
    assert m["units"] == ["a.txt", "b.txt"]
    assert m["title_page"] == "T"
    assert m["separator"] == "|"


def test_parses_json_assembly_label():
    m = assembly.parse_assembly_manifest(
        "```json assembly\n{\"units\": [\"a.txt\"]}\n```"
    )
    assert m is not None and m["units"] == ["a.txt"]


def test_no_fence_returns_none():
    assert assembly.parse_assembly_manifest("just prose, no block") is None


def test_missing_units_returns_none():
    assert assembly.parse_assembly_manifest(_fence('{"title_page": "T"}')) is None


def test_empty_units_returns_none():
    assert assembly.parse_assembly_manifest(_fence('{"units": []}')) is None


def test_non_string_units_returns_none():
    assert assembly.parse_assembly_manifest(_fence('{"units": [1, 2]}')) is None


def test_blank_string_unit_returns_none():
    assert assembly.parse_assembly_manifest(_fence('{"units": ["a.txt", "  "]}')) is None


def test_malformed_json_returns_none():
    assert assembly.parse_assembly_manifest(_fence('{units: not json}')) is None


def test_empty_text_returns_none():
    assert assembly.parse_assembly_manifest("") is None


# ── assembly (concatenation) ──────────────────────────────────────────────


def _units(tmp: Path, **files: str) -> None:
    for name, content in files.items():
        (tmp / name).write_text(content)


def test_concatenates_in_order_with_separator(tmp_path):
    _units(tmp_path, **{"a.txt": "AAA", "b.txt": "BBB", "c.txt": "CCC"})
    r = assembly.assemble(
        {"units": ["c.txt", "a.txt", "b.txt"], "separator": "\n--\n"}, tmp_path
    )
    assert r.content == "CCC\n--\nAAA\n--\nBBB"
    assert r.units_used == ["c.txt", "a.txt", "b.txt"]
    assert r.missing == [] and r.errors == []


def test_title_and_trailer_framing(tmp_path):
    _units(tmp_path, **{"a.txt": "BODY"})
    r = assembly.assemble(
        {"units": ["a.txt"], "title_page": "TITLE", "trailer": "END", "separator": "|"},
        tmp_path,
    )
    assert r.content == "TITLE|BODY|END"


def test_default_separator_when_omitted(tmp_path):
    _units(tmp_path, **{"a.txt": "A", "b.txt": "B"})
    r = assembly.assemble({"units": ["a.txt", "b.txt"]}, tmp_path)
    assert "A" in r.content and "B" in r.content and "---" in r.content


def test_missing_unit_recorded_not_fabricated(tmp_path):
    _units(tmp_path, **{"a.txt": "A"})
    r = assembly.assemble({"units": ["a.txt", "ghost.txt"], "separator": "|"}, tmp_path)
    assert r.units_used == ["a.txt"]
    assert r.missing == ["ghost.txt"]
    assert r.content == "A"  # only the real unit; no fabricated body


# ── path safety (producer-named units are untrusted) ──────────────────────


def test_rejects_parent_traversal(tmp_path):
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("TOPSECRET")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _units(artifacts, **{"a.txt": "A"})
    r = assembly.assemble({"units": ["a.txt", "../secret.txt"]}, artifacts)
    assert "TOPSECRET" not in r.content
    assert "../secret.txt" in r.missing
    assert any("unsafe" in e for e in r.errors)


def test_rejects_absolute_path(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _units(artifacts, **{"a.txt": "A"})
    r = assembly.assemble({"units": ["/etc/passwd"]}, artifacts)
    assert r.units_used == []
    assert "/etc/passwd" in r.missing


def test_rejects_oversize_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(assembly, "_MAX_UNIT_BYTES", 4)
    _units(tmp_path, **{"a.txt": "way too long"})
    r = assembly.assemble({"units": ["a.txt"]}, tmp_path)
    assert r.units_used == []
    assert "a.txt" in r.missing
    assert any("cap" in e for e in r.errors)


def test_allows_nested_unit_path(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("NESTED")
    r = assembly.assemble({"units": ["sub/a.txt"]}, tmp_path)
    assert r.content == "NESTED" and r.units_used == ["sub/a.txt"]
