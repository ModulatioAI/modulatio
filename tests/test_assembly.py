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

import pytest

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


# ── Part B4: media-assembly (local compositors) ───────────────────────────


def test_media_strategy_registered():
    assert "media" in assembly._STRATEGIES


def test_media_bundle_zips_units(tmp_path):
    """Heterogeneous units → one zip via stdlib (no external tool, always works)."""
    import zipfile
    (tmp_path / "a.txt").write_text("ALPHA")
    (tmp_path / "b.png").write_bytes(b"\x89PNG fake bytes")
    r = assembly.assemble(
        {"units": ["a.txt", "b.png"], "media_kind": "bundle"}, tmp_path, strategy="media",
    )
    assert r.errors == [] and r.missing == []
    assert r.units_used == ["a.txt", "b.png"]
    assert "bundle" in r.content  # receipt, not the bytes
    assert r.output_file is not None and r.output_file.is_file()
    with zipfile.ZipFile(r.output_file) as zf:
        assert set(zf.namelist()) == {"a.txt", "b.png"}
        assert zf.read("a.txt") == b"ALPHA"


def test_media_av_fails_closed_without_ffmpeg(tmp_path, monkeypatch):
    """video/audio with ffmpeg ABSENT → fail closed (no output_file, clear error,
    routes to normal review). The CI install-smoke condition."""
    monkeypatch.setattr(assembly.shutil, "which", lambda _name: None)
    (tmp_path / "a.mp4").write_bytes(b"fake mp4")
    (tmp_path / "b.mp4").write_bytes(b"fake mp4 two")
    r = assembly.assemble(
        {"units": ["a.mp4", "b.mp4"], "media_kind": "video"}, tmp_path, strategy="media",
    )
    assert r.content == "" and r.output_file is None
    assert any("ffmpeg" in e for e in r.errors)


def test_media_image_fails_closed_without_imagemagick(tmp_path, monkeypatch):
    monkeypatch.setattr(assembly.shutil, "which", lambda _name: None)
    (tmp_path / "a.png").write_bytes(b"\x89PNG a")
    (tmp_path / "b.png").write_bytes(b"\x89PNG b")
    r = assembly.assemble({"units": ["a.png", "b.png"]}, tmp_path, strategy="media")
    assert r.content == "" and r.output_file is None
    assert any("ImageMagick" in e for e in r.errors)


def test_media_kind_inferred_from_extensions(tmp_path, monkeypatch):
    """No media_kind → inferred from units. All-.mp3 → audio (then fails closed
    without ffmpeg, proving it took the audio path, not bundle)."""
    monkeypatch.setattr(assembly.shutil, "which", lambda _name: None)
    (tmp_path / "a.mp3").write_bytes(b"ID3 fake")
    (tmp_path / "b.mp3").write_bytes(b"ID3 fake2")
    r = assembly.assemble({"units": ["a.mp3", "b.mp3"]}, tmp_path, strategy="media")
    # audio path → ffmpeg → absent → fail closed (a bundle would have SUCCEEDED)
    assert r.content == "" and any("ffmpeg" in e for e in r.errors)


def test_media_unsafe_unit_rejected(tmp_path):
    secret = tmp_path.parent / "secret.bin"
    secret.write_bytes(b"TOPSECRET")
    artifacts = tmp_path / "art"
    artifacts.mkdir()
    (artifacts / "a.txt").write_text("ok")
    r = assembly.assemble(
        {"units": ["a.txt", "../secret.bin"], "media_kind": "bundle"},
        artifacts, strategy="media",
    )
    assert "../secret.bin" in r.missing
    import zipfile
    with zipfile.ZipFile(r.output_file) as zf:
        assert "../secret.bin" not in zf.namelist() and "secret.bin" not in zf.namelist()


def test_media_no_units_fails_closed(tmp_path):
    r = assembly.assemble({"units": ["ghost.png"], "media_kind": "image"}, tmp_path,
                          strategy="media")
    assert r.content == "" and r.output_file is None and r.missing == ["ghost.png"]


def test_media_bundle_over_cap_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(assembly, "_MAX_TOTAL_BYTES", 8)
    (tmp_path / "a.txt").write_text("this content zips to well over eight bytes")
    r = assembly.assemble({"units": ["a.txt"], "media_kind": "bundle"}, tmp_path,
                          strategy="media")
    assert r.content == "" and r.output_file is None
    assert any("exceeds" in e for e in r.errors)


@pytest.mark.skipif(
    __import__("shutil").which("ffmpeg") is None, reason="needs ffmpeg for a live concat",
)
def test_media_av_concat_live(tmp_path):
    """Live ffmpeg concat of two real wavs → one wav. Skipped where ffmpeg absent."""
    import wave
    for n in ("a.wav", "b.wav"):
        with wave.open(str(tmp_path / n), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x01" * 4000)  # 0.5s of samples
    r = assembly.assemble(
        {"units": ["a.wav", "b.wav"], "media_kind": "audio"}, tmp_path, strategy="media",
    )
    assert r.errors == [], r.errors
    assert r.output_file is not None and r.output_file.is_file()
    assert r.output_file.stat().st_size > 0


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
    # framing alone over-cap → framing dropped + error logged (→ complete=False →
    # full review); the units that DO fit still assemble.
    assert r.content == "unit"


# ── Nemo hull review fixes (2026-06-04) ───────────────────────────────────


def test_document_separator_over_cap_returns_empty(tmp_path, monkeypatch):
    """Separator x N blocks counts toward the cap; over-cap → content='' (not the
    oversized bytes)."""
    monkeypatch.setattr(assembly, "_MAX_TOTAL_BYTES", 50)
    for n in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / n).write_text("unit")
    r = assembly.assemble(
        {"units": ["a.txt", "b.txt", "c.txt"], "separator": "Z" * 40}, tmp_path,
    )
    assert r.content == "" and any("exceed" in e for e in r.errors)


def test_data_json_over_cap_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(assembly, "_MAX_TOTAL_BYTES", 30)
    (tmp_path / "a.json").write_text('[{"a": 1}, {"b": 2}, {"c": 3}]')
    r = assembly.assemble({"units": ["a.json"], "format": "json"}, tmp_path, strategy="data")
    assert r.content == "" and any("exceeds" in e for e in r.errors)


def test_csv_strict_rejects_unterminated_quote(tmp_path):
    (tmp_path / "a.csv").write_text('id,name\n1,"unterminated\n')
    r = assembly.assemble({"units": ["a.csv"], "format": "csv"}, tmp_path, strategy="data")
    assert any("invalid CSV" in e for e in r.errors)


def test_csv_row_arity_mismatch_is_error(tmp_path):
    (tmp_path / "a.csv").write_text("id,name\n1,alice\n2,bob,extra\n")
    r = assembly.assemble({"units": ["a.csv"], "format": "csv"}, tmp_path, strategy="data")
    assert any("arity" in e for e in r.errors)


def test_safe_unit_path_rejects_control_chars(tmp_path):
    """Nemo B4 #5: a unit name with a newline/NUL is rejected (it could inject a
    `file '...'` directive into ffmpeg's line-oriented concat list)."""
    assert assembly._safe_unit_path("a\nb.mp4", tmp_path) is None
    assert assembly._safe_unit_path("a\rb.mp4", tmp_path) is None
    assert assembly._safe_unit_path("a\x00b.mp4", tmp_path) is None
    # a normal nested name still resolves
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "ok.mp4").write_text("x")
    assert assembly._safe_unit_path("sub/ok.mp4", tmp_path) is not None
