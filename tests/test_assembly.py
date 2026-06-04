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


# ── Part B: strategy dispatch ─────────────────────────────────────────────


def test_assemble_default_strategy_is_document(tmp_path):
    (tmp_path / "a.txt").write_text("A")
    (tmp_path / "b.txt").write_text("B")
    r = assembly.assemble({"units": ["a.txt", "b.txt"], "separator": "|"}, tmp_path)
    assert r.content == "A|B"  # document concat, the default


def test_assemble_unknown_strategy_fails_closed(tmp_path):
    (tmp_path / "a.txt").write_text("A")
    r = assembly.assemble({"units": ["a.txt"]}, tmp_path, strategy="bogus")
    assert r.content == "" and "unknown assembly strategy 'bogus'" in r.errors[0]
    assert r.missing == ["a.txt"]


def test_document_strategy_registered():
    assert "document" in assembly._STRATEGIES


# ── Part B: code-assembly strategy (index, not concat) ────────────────────


def test_assemble_code_generates_index_not_concat(tmp_path):
    (tmp_path / "main.py").write_text("print('hi')")
    (tmp_path / "util.py").write_text("def f(): pass")
    r = assembly.assemble(
        {"units": ["main.py", "util.py"], "title_page": "MyApp", "entrypoint": "main.py"},
        tmp_path, strategy="code",
    )
    # the index lists the files + entry point; it does NOT contain the source
    assert "main.py" in r.content and "util.py" in r.content
    assert "MyApp" in r.content and "Entry point" in r.content
    assert "print('hi')" not in r.content  # source NOT concatenated into the blob
    assert r.units_used == ["main.py", "util.py"] and r.missing == []
    # the files stay separate on disk, byte-for-byte
    assert (tmp_path / "main.py").read_text() == "print('hi')"


def test_assemble_code_records_missing(tmp_path):
    (tmp_path / "main.py").write_text("x")
    r = assembly.assemble({"units": ["main.py", "gone.py"]}, tmp_path, strategy="code")
    assert r.units_used == ["main.py"] and r.missing == ["gone.py"]


def test_code_strategy_registered():
    assert "code" in assembly._STRATEGIES


# ── Part B: data-assembly strategy (merge/fold) ───────────────────────────


def test_assemble_data_merges_json_arrays(tmp_path):
    (tmp_path / "a.json").write_text('[{"id": 1}, {"id": 2}]')
    (tmp_path / "b.json").write_text('[{"id": 3}]')
    r = assembly.assemble({"units": ["a.json", "b.json"], "format": "json"},
                          tmp_path, strategy="data")
    import json as _json
    assert _json.loads(r.content) == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert r.units_used == ["a.json", "b.json"] and r.errors == []


def test_assemble_data_json_object_becomes_one_record(tmp_path):
    (tmp_path / "a.json").write_text('{"id": 1}')
    r = assembly.assemble({"units": ["a.json"]}, tmp_path, strategy="data")
    import json as _json
    assert _json.loads(r.content) == [{"id": 1}]  # inferred json from extension


def test_assemble_data_json_dedupe(tmp_path):
    (tmp_path / "a.json").write_text('[{"x": 1}, {"x": 1}]')
    (tmp_path / "b.json").write_text('[{"x": 1}, {"x": 2}]')
    r = assembly.assemble({"units": ["a.json", "b.json"], "dedupe": True},
                          tmp_path, strategy="data")
    import json as _json
    assert _json.loads(r.content) == [{"x": 1}, {"x": 2}]


def test_assemble_data_merges_csv_one_header(tmp_path):
    (tmp_path / "a.csv").write_text("id,name\n1,alice\n2,bob\n")
    (tmp_path / "b.csv").write_text("id,name\n3,carol\n")
    r = assembly.assemble({"units": ["a.csv", "b.csv"]}, tmp_path, strategy="data")
    assert r.content == "id,name\n1,alice\n2,bob\n3,carol\n"  # inferred csv


def test_assemble_data_csv_dedupe(tmp_path):
    (tmp_path / "a.csv").write_text("id\n1\n2\n")
    (tmp_path / "b.csv").write_text("id\n2\n3\n")
    r = assembly.assemble({"units": ["a.csv", "b.csv"], "dedupe": True},
                          tmp_path, strategy="data")
    assert r.content == "id\n1\n2\n3\n"


def test_assemble_data_invalid_json_is_an_error(tmp_path):
    (tmp_path / "a.json").write_text("not json{")
    r = assembly.assemble({"units": ["a.json"], "format": "json"}, tmp_path, strategy="data")
    assert any("invalid JSON" in e for e in r.errors)  # -> incomplete -> no cheap QC


def test_data_strategy_registered():
    assert "data" in assembly._STRATEGIES


def test_assemble_media_is_registered_seam_fails_closed(tmp_path):
    (tmp_path / "a.png").write_text("fake")
    r = assembly.assemble({"units": ["a.png"]}, tmp_path, strategy="media")
    assert r.content == "" and "Part B4" in r.errors[0] and r.missing == ["a.png"]


# ── security/debug review fixes (2026-06-04) ──────────────────────────────


def test_merge_json_recursionerror_is_caught(tmp_path):
    """Deeply-nested JSON raises RecursionError (not ValueError) on parse OR
    serialize — it must be caught + recorded, not escape the merge (→ incomplete
    → full review)."""
    (tmp_path / "a.json").write_text("[" * 2000 + "]" * 2000)
    r = assembly.assemble({"units": ["a.json"], "format": "json"}, tmp_path, strategy="data")
    assert any("RecursionError" in e for e in r.errors)  # no crash, recorded


def test_csv_merge_uses_csv_module_quoted_newline(tmp_path):
    """A quoted field with an embedded newline is ONE row, not two (naive
    splitlines corrupts it)."""
    (tmp_path / "a.csv").write_text('id,note\n1,"line1\nline2"\n')
    r = assembly.assemble({"units": ["a.csv"], "format": "csv"}, tmp_path, strategy="data")
    import csv as _csv
    import io as _io
    rows = list(_csv.reader(_io.StringIO(r.content)))
    assert rows == [["id", "note"], ["1", "line1\nline2"]]  # 2 rows, not 3
    assert r.errors == []


def test_csv_header_mismatch_is_an_error(tmp_path):
    (tmp_path / "a.csv").write_text("id,name\n1,alice\n")
    (tmp_path / "b.csv").write_text("id,email\n2,bob@x\n")
    r = assembly.assemble({"units": ["a.csv", "b.csv"], "format": "csv"}, tmp_path, strategy="data")
    assert any("header mismatch" in e for e in r.errors)  # -> incomplete -> no cheap pass


def test_document_framing_cannot_exceed_total_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(assembly, "_MAX_TOTAL_BYTES", 100)
    (tmp_path / "a.txt").write_text("unit")
    r = assembly.assemble(
        {"units": ["a.txt"], "title_page": "X" * 500, "trailer": "Y" * 500}, tmp_path,
    )
    assert any("framing" in e and "cap" in e for e in r.errors)
