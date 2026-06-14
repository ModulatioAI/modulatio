"""0.9.0 pre-ship regression tests for assembly.py findings.

- MEDIUM/resource-leak: PDF render path orphans a partial <stem>.pdf when
  libreoffice fails.
- LOW/correctness: CSV dedupe key can collide across differently-shaped rows.
"""

import json

from modulatio import assembly


def _pdf_artifacts(root):
    return sorted(p.name for p in root.iterdir() if p.suffix == ".pdf")


def test_pdf_render_failure_leaves_no_orphan_pdf(tmp_path, monkeypatch):
    """When soffice 'succeeds' but writes a partial PDF and the size check (or a
    later failure) rejects it, no <stem>.pdf is orphaned in artifacts_root."""
    calls = {"n": 0}

    def fake_run_doc_tool(argv, tool):
        calls["n"] += 1
        if tool == "pandoc":
            # pandoc writes the intermediate .docx
            out = argv[argv.index("-o") + 1]
            assembly.Path(out).write_bytes(b"PK\x03\x04 fake docx")
            return
        # libreoffice: simulate writing a partial PDF into outdir, then fail
        outdir = assembly.Path(argv[argv.index("--outdir") + 1])
        docx = assembly.Path(argv[-1])
        (outdir / docx.with_suffix(".pdf").name).write_bytes(b"%PDF partial")
        raise assembly._DocToolError("libreoffice failed — boom")

    monkeypatch.setattr(assembly, "_run_doc_tool", fake_run_doc_tool)

    try:
        assembly.render_document("# x\n\nbody\n", "pdf", tmp_path)
    except assembly._DocToolError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected _DocToolError")

    assert _pdf_artifacts(tmp_path) == [], (
        "partial PDF was orphaned in artifacts_root on soffice failure"
    )


def test_pdf_render_oversize_leaves_no_orphan_pdf(tmp_path, monkeypatch):
    """An over-cap PDF must be unlinked, not left behind."""
    def fake_run_doc_tool(argv, tool):
        if tool == "pandoc":
            out = argv[argv.index("-o") + 1]
            assembly.Path(out).write_bytes(b"PK\x03\x04 fake docx")
            return
        outdir = assembly.Path(argv[argv.index("--outdir") + 1])
        docx = assembly.Path(argv[-1])
        (outdir / docx.with_suffix(".pdf").name).write_bytes(b"%PDF over cap")

    monkeypatch.setattr(assembly, "_run_doc_tool", fake_run_doc_tool)
    monkeypatch.setattr(assembly, "_MAX_TOTAL_BYTES", 1)  # force over-cap

    try:
        assembly.render_document("# x\n\nbody\n", "pdf", tmp_path)
    except (assembly._DocToolError, assembly._MediaToolError):
        pass
    else:  # pragma: no cover
        raise AssertionError("expected a tool error on over-cap output")

    assert _pdf_artifacts(tmp_path) == [], "over-cap PDF was orphaned"


def test_csv_dedupe_does_not_collide_on_nul_shifted_values():
    """Two genuinely-distinct rows whose values differ only by where a NUL byte
    falls must NOT collapse to one row under dedupe (the old "\\x00".join key
    aliased them)."""
    csv_a = "h1,h2\r\n" + 'a\x00b,c\r\n' + 'a,b\x00c\r\n'
    content, errors = assembly._merge_csv([("u", csv_a)], dedupe=True)

    assert errors == [], errors
    # Header + two distinct data rows must survive dedupe.
    body_lines = [ln for ln in content.splitlines() if ln]
    assert len(body_lines) == 3, (content, body_lines)


def test_csv_dedupe_still_collapses_true_duplicates():
    """Dedupe must still remove an exact duplicate row."""
    csv_text = "h1,h2\n" + "x,y\n" + "x,y\n" + "p,q\n"
    content, errors = assembly._merge_csv([("u", csv_text)], dedupe=True)
    assert errors == []
    body_lines = [ln for ln in content.splitlines() if ln]
    # header + (x,y) + (p,q) == 3
    assert len(body_lines) == 3, body_lines


def test_csv_dedupe_key_is_unambiguous_serialization():
    """Sanity: the dedupe serialization round-trips field boundaries."""
    assert json.loads(json.dumps(["a\x00b", "c"])) == ["a\x00b", "c"]
