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


# ── P4: document family renders a declared binary (artifact-agnostic) ──────


def test_assemble_document_render_fail_closed_keeps_text(tmp_path, monkeypatch):
    """P4: a GENUINELY-absent render toolchain must NOT fabricate a binary — it
    keeps the REAL assembled text and flags the binary as unrendered (anti-HRWT).
    'Absent' now means resolve_tool finds nothing (PATH *and* the search dirs)."""
    (tmp_path / "u1.md").write_text("# One\n\nbody one\n")
    (tmp_path / "u2.md").write_text("# Two\n\nbody two\n")
    monkeypatch.setattr(assembly, "resolve_tool", lambda _name: None)
    res = assembly.assemble(
        {"units": ["u1.md", "u2.md"]}, tmp_path, strategy="document",
        render_format="docx",
    )
    assert res.output_file is None  # no fabricated binary
    assert "body one" in res.content and "body two" in res.content  # real text kept
    assert any("binary render unavailable" in e for e in res.errors)


def test_render_document_raises_without_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(assembly, "resolve_tool", lambda _name: None)
    with pytest.raises(assembly._DocToolError):
        assembly.render_document("# x\n\nbody\n", "docx", tmp_path)


def test_assemble_document_no_render_format_stays_text(tmp_path):
    """No declared format → the body stays text; no binary imposed (agnostic)."""
    (tmp_path / "u1.md").write_text("alpha")
    res = assembly.assemble({"units": ["u1.md"]}, tmp_path, strategy="document")
    assert res.output_file is None
    assert res.content == "alpha"


@pytest.mark.skipif(
    __import__("shutil").which("pandoc") is None, reason="pandoc not installed"
)
def test_assemble_document_renders_real_docx(tmp_path):
    """With pandoc present, a document assembly renders a REAL .docx (zip magic),
    not a text blob named .docx."""
    (tmp_path / "u1.md").write_text("# One\n\nbody one\n")
    (tmp_path / "u2.md").write_text("# Two\n\nbody two\n")
    res = assembly.assemble(
        {"units": ["u1.md", "u2.md"]}, tmp_path, strategy="document",
        render_format="docx",
    )
    assert res.output_file is not None and res.output_file.is_file()
    assert res.output_file.read_bytes()[:4] == b"PK\x03\x04"  # real .docx = zip


# ── robust engine-tool discovery (HRWT pandoc-in-~/bin failure) ────────────


def test_resolve_tool_env_override(monkeypatch, tmp_path):
    """An explicit MODULATIO_<NAME>_PATH override wins and is returned verbatim."""
    fake = tmp_path / "mytool"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("MODULATIO_MY_TOOL_PATH", str(fake))
    assert assembly.resolve_tool("my-tool") == str(fake)


def test_resolve_tool_found_in_search_dir_when_off_path(monkeypatch):
    """The whole point: a tool NOT on PATH is still found via the common install
    dirs (~/bin, /bin, /usr/local/bin…). Simulate the HRWT case — PATH lookup
    fails, but the engine still resolves the tool to an ABSOLUTE executable.
    (resolve() canonicalizes symlinks, so assert is_file, not the basename — sh is
    commonly a symlink to dash.)"""
    from pathlib import Path as _P
    import shutil as _sh
    monkeypatch.setattr(assembly, "shutil", _sh)
    monkeypatch.setattr(_sh, "which", lambda _n: None)  # PATH misses it
    resolved = assembly.resolve_tool("sh")  # /bin in _TOOL_SEARCH_DIRS (curated)
    assert resolved is not None
    assert _P(resolved).is_absolute() and _P(resolved).is_file()


def test_resolve_tool_rejects_relative_override(monkeypatch, tmp_path):
    """Nemo hull #6: a RELATIVE override is rejected (would re-introduce cwd
    dependence) — a set-but-unusable override is a HARD STOP, not a fall-through."""
    monkeypatch.setenv("MODULATIO_MY_TOOL_PATH", "./mytool")  # relative
    assert assembly.resolve_tool("my-tool") is None


def test_resolve_tool_curated_dirs_before_path(monkeypatch, tmp_path):
    """Nemo hull #7: curated absolute dirs are checked BEFORE PATH, so a
    contaminated PATH cannot shadow a real system binary. A fake 'sh' planted on a
    PATH dir must NOT win over /bin/sh."""
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "sh").write_text("#!/bin/sh\necho pwned\n")
    (evil / "sh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{evil}:/usr/bin:/bin")
    resolved = assembly.resolve_tool("sh")
    # Resolves to a curated /bin or /usr/bin sh, never the tmp 'evil' one.
    assert resolved is not None and str(tmp_path) not in resolved


def test_resolve_tool_none_when_absent(monkeypatch):
    import shutil as _sh
    monkeypatch.setattr(_sh, "which", lambda _n: None)
    assert assembly.resolve_tool("definitely-not-a-real-binary-xyz123") is None


def test_render_doc_tool_failclosed_message_names_override(tmp_path, monkeypatch):
    """When a render tool is genuinely unfindable, the error names the override
    env var so the operator knows how to point the engine at it."""
    monkeypatch.setattr(assembly, "resolve_tool", lambda _n: None)
    with pytest.raises(assembly._DocToolError) as exc:
        assembly.render_document("# x\n\nbody\n", "docx", tmp_path)
    assert "MODULATIO_PANDOC_PATH" in str(exc.value)


# ── #101 Part 0: the deliverable digest (the verifier's eyes) ─────────────────

def test_document_digest_parts_label_and_size(tmp_path):
    _units(tmp_path, **{
        # whitespace word count is the document family's part-size unit; the leading
        # "#" token counts (consistent with the engine's word-count convention)
        "s1.md": "# Chapter One\n\nalpha beta gamma",       # label "Chapter One", 6 words
        "s2.md": "Chapter Two\n\nword word",                # label "Chapter Two", 4 words
    })
    d = assembly.build_deliverable_digest(
        {"units": ["s1.md", "s2.md"]}, ["s1.md", "s2.md"], tmp_path, strategy="document")
    assert d.kind == "document"
    assert d.part_count == 2
    assert d.part_size_unit == "words"
    assert [p["label"] for p in d.parts] == ["Chapter One", "Chapter Two"]
    assert [p["size"] for p in d.parts] == [6, 4]
    assert d.whole_size is None and d.whole_size_unit == "pages"   # no rendered PDF
    assert d.text_twin_path is None


def test_document_digest_structure_flags(tmp_path):
    _units(tmp_path, **{"a.md": "Body\n\ntext"})
    d = assembly.build_deliverable_digest(
        {"units": ["a.md"], "title_page": "My Anthology", "toc": True},
        ["a.md"], tmp_path, strategy="document")
    assert d.structure == {"title": True, "toc": True}
    d2 = assembly.build_deliverable_digest(
        {"units": ["a.md"]}, ["a.md"], tmp_path, strategy="document")
    assert d2.structure == {"title": False, "toc": False}


# ── #101 Part A: engine-supplied framing (per-family head dispatch) ────────────


def test_apply_framing_document_generates_title_and_toc(tmp_path):
    """The document head renderer builds a title + a TOC from the unit headings into
    title_page (which the assembler prepends) and flags toc."""
    _units(tmp_path, **{"s1.md": "# Story One\n\nbody", "s2.md": "# Story Two\n\nbody"})
    m = assembly.apply_framing(
        {"units": ["s1.md", "s2.md"]}, tmp_path, "document",
        title="My Anthology", required_structure=("title", "toc"))
    tp = m["title_page"]
    assert tp.startswith("# My Anthology")
    assert "## Contents" in tp and "1. Story One" in tp and "2. Story Two" in tp
    assert m["toc"] is True


def test_apply_framing_closes_loop_digest_recognizes_structure(tmp_path):
    """Part A + B.2 loop: after the engine frames the manifest, the digest reports the
    declared structure as PRESENT — so B.2's required_structure check passes."""
    _units(tmp_path, **{"s1.md": "# One\n\nx", "s2.md": "# Two\n\ny"})
    m = assembly.apply_framing(
        {"units": ["s1.md", "s2.md"]}, tmp_path, "document",
        title="Anthology", required_structure=("title", "toc"))
    d = assembly.build_deliverable_digest(m, ["s1.md", "s2.md"], tmp_path,
                                          strategy="document")
    assert d.structure == {"title": True, "toc": True}


def test_apply_framing_respects_producer_title_page(tmp_path):
    """Producer-authored framing wins — a non-empty title_page is never overridden."""
    _units(tmp_path, **{"s1.md": "# One\n\nx"})
    m = assembly.apply_framing(
        {"units": ["s1.md"], "title_page": "PRODUCER FRAME"}, tmp_path, "document",
        title="Engine Title", required_structure=("title", "toc"))
    assert m["title_page"] == "PRODUCER FRAME"
    assert "toc" not in m


def test_apply_framing_non_document_family_is_noop(tmp_path):
    """A family with no head renderer (media/code/data) gets NO engine head — a
    document-style title is never forced onto a video/app/dataset."""
    _units(tmp_path, **{"clip1.mp4": "x", "clip2.mp4": "y"})
    manifest = {"units": ["clip1.mp4", "clip2.mp4"]}
    m = assembly.apply_framing(manifest, tmp_path, "media",
                               title="My Film", required_structure=("title", "toc"))
    assert m == manifest and "title_page" not in m   # untouched


def test_apply_framing_no_declared_framing_is_noop(tmp_path):
    """No declared title/structure (the empty-spec default) → manifest unchanged."""
    _units(tmp_path, **{"s1.md": "# One\n\nx"})
    manifest = {"units": ["s1.md"]}
    m = assembly.apply_framing(manifest, tmp_path, "document",
                               title=None, required_structure=())
    assert m == manifest and "title_page" not in m


def test_apply_framing_title_only_no_toc(tmp_path):
    """Title declared but not toc → a title head, no Contents, no toc flag."""
    _units(tmp_path, **{"s1.md": "# One\n\nx"})
    m = assembly.apply_framing({"units": ["s1.md"]}, tmp_path, "document",
                               title="Just A Title", required_structure=("title",))
    assert m["title_page"] == "# Just A Title"
    assert "Contents" not in m["title_page"] and "toc" not in m


# ── #101 Part D: cross-part continuity normalization (per-family dispatch) ─────


def test_continuity_normalizes_inconsistent_sequence():
    out, changed = assembly.continuity_headings(
        ["Story 1: A", "Story 7: B", "Story 1: C"], "document")
    assert changed and out == ["Story 1: A", "Story 2: B", "Story 3: C"]


def test_continuity_leaves_clean_sequence_untouched():
    hs = ["Chapter 1", "Chapter 2", "Chapter 3"]
    out, changed = assembly.continuity_headings(hs, "document")
    assert not changed and out == hs            # already 1..N — never disturb it


def test_continuity_no_op_when_a_part_is_unlabeled():
    hs = ["Story 1", "An Interlude", "Story 3"]  # middle carries no ordinal
    out, changed = assembly.continuity_headings(hs, "document")
    assert not changed and out == hs            # never partially renumber


def test_continuity_does_not_touch_incidental_numbers():
    out, changed = assembly.continuity_headings(["The 7 Samurai", "Two Towers"], "document")
    assert not changed                          # not sequence markers — left alone


def test_continuity_leading_number_form():
    out, changed = assembly.continuity_headings(["3. Alpha", "9. Beta"], "document")
    assert changed and out == ["1. Alpha", "2. Beta"]


def test_continuity_mixed_labels_is_noop():
    """Nemo follow-up: a heterogeneous label set is not one sequence — leave it untouched
    rather than renumber Story/Chapter/Section into a fake run."""
    hs = ["Story 1: A", "Chapter 7: B", "Section 3: C"]
    out, changed = assembly.continuity_headings(hs, "document")
    assert not changed and out == hs


def test_continuity_label_mixed_with_leading_number_is_noop():
    out, changed = assembly.continuity_headings(["Story 1", "2. B"], "document")
    assert not changed                                  # label form + bare-number form ≠ one family


def test_continuity_non_document_family_is_noop():
    hs = ["Story 1", "Story 7"]
    out, changed = assembly.continuity_headings(hs, "media")
    assert not changed and out == hs            # no normalizer for the family → untouched


def test_replace_first_heading_preserves_hashes():
    assert assembly._replace_first_heading("## Story 7\n\nbody", "Story 2") == \
        "## Story 2\n\nbody"


def test_assemble_document_renumbers_inconsistent_parts(tmp_path):
    _units(tmp_path, **{"a.md": "# Story 1\n\nalpha", "b.md": "# Story 7\n\nbeta",
                        "c.md": "# Story 1\n\ngamma"})
    r = assembly.assemble(
        {"units": ["a.md", "b.md", "c.md"], "separator": "\n\n"}, tmp_path)
    assert "# Story 1" in r.content and "# Story 2" in r.content and "# Story 3" in r.content
    assert "# Story 7" not in r.content                       # the collision is reconciled
    assert "alpha" in r.content and "beta" in r.content and "gamma" in r.content  # bodies kept


def test_assemble_document_leaves_clean_sequence(tmp_path):
    _units(tmp_path, **{"a.md": "# Part 1\n\nx", "b.md": "# Part 2\n\ny"})
    r = assembly.assemble({"units": ["a.md", "b.md"], "separator": "\n\n"}, tmp_path)
    assert "# Part 1" in r.content and "# Part 2" in r.content   # untouched


def test_framing_toc_matches_renumbered_body(tmp_path):
    """Part A + D: the engine-framed TOC lists the SAME normalized sequence the assembled
    body uses — both pass through the one document normalizer."""
    _units(tmp_path, **{"a.md": "# Story 1\n\nx", "b.md": "# Story 7\n\ny"})
    m = assembly.apply_framing({"units": ["a.md", "b.md"]}, tmp_path, "document",
                               title="Anthology", required_structure=("title", "toc"))
    assert "1. Story 1" in m["title_page"] and "2. Story 2" in m["title_page"]
    r = assembly.assemble(m, tmp_path)
    assert "# Story 1" in r.content and "# Story 2" in r.content and "# Story 7" not in r.content


def test_framing_toc_excludes_missing_unit(tmp_path):
    """The TOC must list only units that land in the body — a missing/unresolved unit
    appears in neither (no phantom Contents entry for content that isn't there)."""
    _units(tmp_path, **{"a.md": "# Alpha\n\nx", "c.md": "# Gamma\n\nz"})
    # b.md is declared but never written → missing.
    m = assembly.apply_framing({"units": ["a.md", "b.md", "c.md"]}, tmp_path,
                               "document", title="Anthology",
                               required_structure=("title", "toc"))
    tp = m["title_page"]
    assert "Alpha" in tp and "Gamma" in tp and "Beta" not in tp
    # Exactly two Contents entries, renumbered 1..2 to match the body's two blocks.
    assert "1. Alpha" in tp and "2. Gamma" in tp and "3." not in tp
    r = assembly.assemble(m, tmp_path)
    assert "# Alpha" in r.content and "# Gamma" in r.content
    assert r.missing == ["b.md"]


def test_framing_toc_excludes_oversized_unit(tmp_path, monkeypatch):
    """A unit over the per-unit byte cap is skipped by the body — the TOC must skip it
    too, so the two never diverge."""
    monkeypatch.setattr(assembly, "_MAX_UNIT_BYTES", 20)
    _units(tmp_path, **{
        "a.md": "# Alpha\n\nx",
        "big.md": "# Huge\n\n" + ("y" * 100),   # > 20 bytes → skipped by the body
        "c.md": "# Gamma\n\nz",
    })
    m = assembly.apply_framing({"units": ["a.md", "big.md", "c.md"]}, tmp_path,
                               "document", title="Anthology",
                               required_structure=("title", "toc"))
    tp = m["title_page"]
    assert "Alpha" in tp and "Gamma" in tp and "Huge" not in tp
    r = assembly.assemble(m, tmp_path)
    assert "# Huge" not in r.content
    # TOC headings == body headings (both exclude the oversized unit).
    assert "1. Alpha" in tp and "2. Gamma" in tp


def test_framing_toc_truncates_at_total_cap_like_body(tmp_path, monkeypatch):
    """When the total-byte cap stops the body before the last units, the TOC stops at
    the SAME point — it never lists units the cap truncated out of the body.

    re-sweep (#101/0.9.0, assembly.py:491): the TOC's cap-math must seed with the
    framing the BODY actually counts — the FULL title_page (incl. the rendered TOC
    block) plus a leading separator before unit #1 — not the title line alone. The
    body fail-CLOSES content to empty once any unit is dropped at the cap, so the
    honest comparison is against ``units_used`` (the body's accumulated set, which
    survives the fail-close), and the TOC must be a SUBSET of it — never a phantom
    entry the reader can't find. (The old assertion ``"Alpha" in tp`` baked in the
    very divergence this finding fixes: the TOC listed Alpha while the body kept
    nothing.)"""
    monkeypatch.setattr(assembly, "_MAX_TOTAL_BYTES", 40)
    _units(tmp_path, **{
        "a.md": "# Alpha\n\n" + ("x" * 15),
        "b.md": "# Beta\n\n" + ("y" * 15),
        "c.md": "# Gamma\n\n" + ("z" * 15),   # pushes total over 40 → dropped
    })
    m = assembly.apply_framing(
        {"units": ["a.md", "b.md", "c.md"], "separator": "\n"}, tmp_path,
        "document", title="Anthology", required_structure=("title", "toc"))
    tp = m["title_page"]
    assert "Gamma" not in tp   # the cap-dropped unit is never listed
    r = assembly.assemble(m, tmp_path)
    assert "# Gamma" not in r.content
    # The TOC lists only units that survive into the body's accumulation (units_used),
    # never a unit beyond where the cap stops it.
    toc_listed = {h for h in ("Alpha", "Beta", "Gamma") if h in tp}
    body_units = {assembly._first_heading((tmp_path / u).read_text()) for u in r.units_used}
    assert toc_listed <= body_units


def test_document_digest_fail_open_on_missing_unit(tmp_path):
    d = assembly.build_deliverable_digest(
        {"units": ["ghost.md"]}, ["ghost.md"], tmp_path, strategy="document")
    assert d.part_count == 1
    assert d.parts == [{"label": "", "size": 0}]


def test_document_digest_first_heading_strips_markdown_hashes(tmp_path):
    _units(tmp_path, **{"a.md": "###  Spaced Heading  \n\nbody here"})
    d = assembly.build_deliverable_digest(
        {"units": ["a.md"]}, ["a.md"], tmp_path, strategy="document")
    assert d.parts[0]["label"] == "Spaced Heading"


def test_digest_is_product_agnostic_generic_fallback(tmp_path):
    """A NON-document strategy must NOT get document assumptions — it falls back to a
    family-neutral byte digest (parts sized in bytes, no headings/words/TOC). This is
    the guard against baking one output class into the engine contract."""
    _units(tmp_path, **{"f1.bin": "abcde", "f2.bin": "xy"})
    d = assembly.build_deliverable_digest(
        {"units": ["f1.bin", "f2.bin"]}, ["f1.bin", "f2.bin"], tmp_path, strategy="data")
    assert d.kind == "data"               # echoes the family, no "document" default
    assert d.part_size_unit == "bytes"    # neutral measure, not "words"
    assert [p["size"] for p in d.parts] == [5, 2]
    assert d.structure == {}              # no document framing assumed
    assert d.whole_size is None


def test_write_text_twin_persists_under_twins_dir(tmp_path):
    rel = assembly.write_text_twin("# Bound\n\nreadable body", tmp_path, "DIG-T-001")
    assert rel == ".twins/DIG-T-001.md"
    assert (tmp_path / rel).read_text() == "# Bound\n\nreadable body"


def test_write_text_twin_sanitizes_name_no_traversal(tmp_path):
    rel = assembly.write_text_twin("x", tmp_path, "weird/../name")
    assert rel.startswith(".twins/")
    leaf = rel[len(".twins/"):]
    assert "/" not in leaf                 # path separators stripped — no traversal
    assert (tmp_path / rel).is_file()


def test_format_digest_is_readable_and_product_agnostic():
    # a DATA digest (rows, header_row) — the renderer must carry NO document vocabulary
    d = assembly.DeliverableDigest(
        kind="data", part_count=2,
        parts=[{"label": "users.csv", "size": 1000}, {"label": "orders.csv", "size": 50}],
        part_size_unit="rows", structure={"header_row": True},
        whole_size=1050, whole_size_unit="rows")
    s = assembly.format_digest(d)
    assert "kind=data, parts=2" in s
    assert "'users.csv' — 1000 rows" in s
    assert "header_row=True" in s
    assert "whole size: 1050 rows" in s
    low = s.lower()
    assert "word" not in low and "page" not in low and "toc" not in low


def _digest(parts, **kw):
    return assembly.DeliverableDigest(
        kind=kw.get("kind", "document"), part_count=len(parts), parts=parts,
        part_size_unit=kw.get("unit", "words"), structure=kw.get("structure", {}))


def test_check_deliverable_clean_passes():
    d = _digest([{"label": "One", "size": 2500}, {"label": "Two", "size": 2200}],
                structure={"title": True, "toc": True})
    assert assembly.check_deliverable(
        d, expected_count=2, part_floor=2000, required_structure=("title", "toc")) == []


def test_check_deliverable_flags_hrwt_failures():
    """Count + under-length + missing framing — the HRWT failures, caught by arithmetic."""
    d = _digest([{"label": "One", "size": 2692}, {"label": "Two", "size": 906}],  # 1 short
                structure={"title": False, "toc": False})
    issues = assembly.check_deliverable(
        d, expected_count=8, part_floor=2000, required_structure=("title", "toc"))
    assert any("expected 8 parts, got 2" in i for i in issues)
    assert any("under the 2000-words floor" in i and "Two" in i for i in issues)
    assert any("title" in i for i in issues) and any("toc" in i for i in issues)


def test_check_deliverable_flags_blank_label():
    d = _digest([{"label": "", "size": 100}])
    assert any("no label/heading" in i for i in assembly.check_deliverable(d))


def test_check_deliverable_is_product_agnostic_rows():
    # a DATA digest, floor in ROWS — the check needs no document vocabulary
    d = assembly.DeliverableDigest(
        kind="data", part_count=2,
        parts=[{"label": "users", "size": 1000}, {"label": "orders", "size": 5}],
        part_size_unit="rows")
    issues = assembly.check_deliverable(d, part_floor=10)
    assert any("under the 10-rows floor" in i and "orders" in i for i in issues)


# ── leading producer-scaffold strip (run-1 gaming-report leak) ──────────────


def test_strips_runbook_preamble_before_first_heading(tmp_path):
    leaked = (
        "I now have all the data I need. Let me write the corrected artifact.\n\n"
        "**Operation:** Produce Research Note\n"
        "**Definition of Done:** A concise research note.\n\n"
        "# Research Note: Pricing\n\nBody stays.\n"
    )
    _units(tmp_path, **{"a.md": leaked, "b.md": "# Clean\n\nAlso stays."})
    r = assembly.assemble({"units": ["a.md", "b.md"], "separator": "|"}, tmp_path)
    assert r.content.startswith("# Research Note: Pricing")
    assert "Operation:" not in r.content
    assert "Definition of Done" not in r.content
    assert "all the data I need" not in r.content
    assert "Body stays." in r.content and "Also stays." in r.content


def test_plain_prose_intro_before_heading_is_kept(tmp_path):
    body = "A real opening paragraph, no scaffold here.\n\n# Section\n\nContent.\n"
    _units(tmp_path, **{"a.md": body})
    r = assembly.assemble({"units": ["a.md"]}, tmp_path)
    assert "A real opening paragraph" in r.content


def test_runbook_text_after_first_heading_is_kept(tmp_path):
    body = "# Producer Guide\n\nReplies carry an **Operation:** line up front.\n"
    _units(tmp_path, **{"a.md": body})
    r = assembly.assemble({"units": ["a.md"]}, tmp_path)
    assert "**Operation:**" in r.content


def test_no_heading_leaves_body_untouched(tmp_path):
    body = "**Operation:** Do a thing\n\nJust prose, never a heading.\n"
    _units(tmp_path, **{"a.md": body})
    r = assembly.assemble({"units": ["a.md"]}, tmp_path)
    assert r.content == body.strip("\n")


def test_deep_first_heading_beyond_scan_range_is_kept(tmp_path):
    body = (
        "Operation: mentioned in ordinary prose\n"
        + "filler line\n" * 40
        + "# Late heading\n\nBody.\n"
    )
    _units(tmp_path, **{"a.md": body})
    r = assembly.assemble({"units": ["a.md"]}, tmp_path)
    assert "Operation: mentioned in ordinary prose" in r.content
