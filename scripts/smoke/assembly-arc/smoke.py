#!/usr/bin/env python3
"""Smoke: the deterministic-assembly arc (P1/P4/P5 + tool discovery).

Run from the repo root:  .venv/bin/python scripts/smoke/assembly-arc/smoke.py

Each check prints PASS/FAIL. Exit code is non-zero if any FAIL. These exercise the
LIVE-found failure modes directly — render off-PATH, the magic-byte gate, fail-closed
without a tool — so a reviewer can re-prove the claims without the unit harness.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from modulatio import assembly, review_ledger  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        fails += 1


print("== assembly-arc smoke ==")

# 1. resolve_tool finds a tool OFF PATH (the HRWT pandoc-in-~/bin failure).
with tempfile.TemporaryDirectory() as d:
    # /bin/sh exists and /bin is a search dir; strip it from PATH so only the
    # search-dir fallback can find it.
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = "/nonexistent"
    try:
        r = assembly.resolve_tool("sh")
    finally:
        os.environ["PATH"] = old
    check("resolve_tool finds 'sh' with PATH stripped (search-dir fallback)",
          r is not None and r.endswith("/sh"), str(r))

# 2. resolve_tool honors an explicit override.
with tempfile.TemporaryDirectory() as d:
    fake = Path(d) / "mytool"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    os.environ["MODULATIO_MY_TOOL_PATH"] = str(fake)
    try:
        ok = assembly.resolve_tool("my-tool") == str(fake)
    finally:
        del os.environ["MODULATIO_MY_TOOL_PATH"]
    check("resolve_tool honors MODULATIO_<NAME>_PATH override", ok)

# 3. P5 magic-byte gate: rejects text-named-.pdf, accepts a real %PDF.
with tempfile.TemporaryDirectory() as d:
    fake_pdf = Path(d) / "book.pdf"
    fake_pdf.write_text("# Collected Stories\n\nnot a pdf, just text\n")
    ok1, _ = review_ledger.verify_declared_format(fake_pdf)
    real_pdf = Path(d) / "real.pdf"
    real_pdf.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\nbody")
    ok2, _ = review_ledger.verify_declared_format(real_pdf)
    md = Path(d) / "report.md"
    md.write_text("anything")
    ok3, _ = review_ledger.verify_declared_format(md)
    check("P5 rejects text-named-.pdf", ok1 is False)
    check("P5 accepts a real %PDF", ok2 is True)
    check("P5 imposes nothing on .md (text/unknown)", ok3 is True)

# 4. Render: real binary when the toolchain is present; fail-closed (raise) when not.
if assembly.resolve_tool("pandoc"):
    with tempfile.TemporaryDirectory() as d:
        out, _msg = assembly.render_document("# Doc\n\nHello.\n", "docx", Path(d))
        b = out.read_bytes()
        check("render md->docx produces a real zip-office binary",
              b[:4] == b"PK\x03\x04", f"{b[:4]!r}")
else:
    print("  [SKIP] pandoc not found — render-real-binary check skipped")

with tempfile.TemporaryDirectory() as d:
    # Genuine absence: monkeypatch resolve_tool -> None so nothing resolves.
    orig = assembly.resolve_tool
    assembly.resolve_tool = lambda _n: None  # type: ignore[assignment]
    try:
        raised = False
        try:
            assembly.render_document("# x\n\nbody\n", "pdf", Path(d))
        except assembly._DocToolError:
            raised = True
    finally:
        assembly.resolve_tool = orig  # type: ignore[assignment]
    check("render fail-closed (raises _DocToolError) when no tool resolves", raised)

# 5. The unit-level guarantee: engine binds in EVERY producer mode (no producer LLM).
res = subprocess.run(
    [str(REPO / ".venv/bin/python"), "-m", "pytest", "-q",
     "tests/test_orchestration.py::test_assembler_engine_binds_in_every_producer_mode"],
    cwd=REPO, capture_output=True, text=True,
)
check("engine binds an assembler in generate/diff/revise/edit/patch (no producer)",
      res.returncode == 0, res.stdout.strip().splitlines()[-1] if res.stdout else "")

print(f"\n{'ALL PASS' if fails == 0 else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
