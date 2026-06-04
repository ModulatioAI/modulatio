# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Mechanical assembly of unit artifacts into one deliverable.

The consolidation problem: a producer asked to assemble N already-produced
units (N stories into a book, N modules into a package, N records into a
dataset) used to be told *"your response IS the assembled output"* — so it
re-emitted every unit's body as LLM output tokens. For anything large that
hits the model's OUTPUT token ceiling and the deliverable comes back
**truncated** (the 2026-06-03 western anthology: 6 stories ≈ 12K output
tokens → only 2 stories survived).

The fix is the speculative-decoding thesis applied to assembly: the smart
model decides the *plan* (cheap output) and the engine executes the *bulk
copy* (free). The producer emits a small **assembly manifest** — a title,
an ordered list of unit filenames (it already sees them in the injected
repo_map), and a separator — and this module reads those unit bodies from
disk and concatenates them. Unit content never round-trips through the
model, so there is no output-cap truncation, no clobbering, and the unit
paths are gated against traversal.

Opt-in: a producer that emits no manifest falls through to the normal
"response IS the artifact" path unchanged. So structured merges (a JSON
fold, a manifest-of-pointers) that genuinely need model authorship are
unaffected — only verbatim concatenation uses this.

Manifest shape — a fenced ``assembly`` block holding JSON::

    ```assembly
    {
      "title_page": "Six-Gun Stories\\n\\nAn Anthology of the Frontier\\n",
      "separator": "\\n\\n---\\n\\n",
      "units": ["story_aging_marshal.txt", "story_hired_gun.txt"],
      "trailer": ""
    }
    ```

``units`` is required (non-empty list of artifacts-relative paths).
``title_page`` / ``separator`` / ``trailer`` are optional. Everything is
plain text, token-native, artifact-agnostic — what a "unit" is is the
caller's business; this module only concatenates bytes in the named order.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

#: Per-unit read ceiling — mirrors run_shell's read cap. A single unit
#: larger than this is almost certainly a mistake; skip it + surface.
_MAX_UNIT_BYTES = 4 * 1024 * 1024
#: Total assembled-output ceiling. Generous (books, merged datasets) but
#: bounded so a runaway manifest can't write an unbounded file.
_MAX_TOTAL_BYTES = 32 * 1024 * 1024

_DEFAULT_SEPARATOR = "\n\n---\n\n"

#: Fenced ``assembly`` block. Tolerant of leading spaces and an optional
#: trailing newline before the closing fence. DOTALL so the JSON body may
#: span lines.
_FENCE_RE = re.compile(
    r"```(?:assembly|json\s+assembly)\s*\n(?P<body>.*?)\n?```",
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class AssemblyResult:
    """Outcome of a mechanical assembly.

    ``content`` is the concatenated deliverable (write it to output_path).
    ``units_used`` / ``missing`` / ``errors`` let the caller surface an
    honest blocker — assembly is best-effort (ship what resolved, name
    what didn't), never a silent drop.
    """

    content: str
    units_used: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class AssemblyRecord:
    """Engine-authored proof that a deliverable was produced by MECHANICAL
    assembly (task #85, Nemo blocker 3). It exists ONLY when the engine ran
    ``assemble()`` — a producer that merely emits assembled-looking text leaves
    no record, so it cannot bypass normal QC. Assembly QC (``verify_assembly``)
    routes to the cheap structural check only when this record exists AND the
    on-disk output still hashes to ``final_checksum``; otherwise it falls back to
    a full normal review (fail-closed).

    Fields:
      manifest      — the producer's plan (units order + framing). UNTRUSTED for
                      the unit SET (verified against the task graph), but it is
                      what the engine actually concatenated.
      final_checksum— ``sha256:<hex>`` of the bytes the engine wrote to
                      output_path. QC recomputes it from disk to detect any
                      post-assembly tampering.
      complete      — True iff every named unit resolved (no missing/errors). A
                      partial assembly is never eligible for the cheap pass.
      strategy / algo_version — which mechanical recipe produced it (Part B grows
                      ``strategy`` past "document"; ``algo_version`` bumps if the
                      deterministic transform changes).
    """

    manifest: dict
    final_checksum: str
    complete: bool
    strategy: str = "document"
    algo_version: str = "1"


def parse_assembly_manifest(text: str) -> dict | None:
    """Return the parsed manifest dict if ``text`` carries a well-formed
    ``assembly`` block with a non-empty ``units`` list, else ``None``.

    Strict on the contract (must have ``units``) but tolerant of the
    producer's formatting (fence casing, whitespace). A malformed JSON
    body or a missing ``units`` key returns ``None`` so the caller falls
    back to the normal artifact path rather than mis-assembling.
    """
    if not text:
        return None
    m = _FENCE_RE.search(text)
    if m is None:
        return None
    raw = m.group("body").strip()
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    units = obj.get("units")
    if not isinstance(units, list) or not units:
        return None
    if not all(isinstance(u, str) and u.strip() for u in units):
        return None
    return obj


def _safe_unit_path(name: str, artifacts_root: Path) -> Path | None:
    """Resolve a manifest unit name under ``artifacts_root``, returning the
    absolute path or ``None`` if it escapes the root, is absolute, or uses
    a dot/parent component. Mirrors the ``write_artifact`` / output_path
    safety gate: producer-named paths are untrusted input.
    """
    stripped = (name or "").strip()
    if not stripped or stripped.startswith("/") or stripped.startswith("~"):
        return None
    root = artifacts_root.resolve()
    candidate = (root / stripped).resolve()
    try:
        rel = candidate.relative_to(root)
    except ValueError:
        return None
    for part in rel.parts:
        if part in ("..", ".") or part.startswith(".."):
            return None
    return candidate


def assemble(
    manifest: dict, artifacts_root: Path, strategy: str = "document"
) -> AssemblyResult:
    """Mechanically assemble the manifest's units per the named STRATEGY (Part B).

    The producer emits the PLAN (manifest); the engine does the bulk join — the
    one invariant across every family is that unit bytes never round-trip through
    the model. The *join* itself differs by family:

      - ``document`` — ordered text concatenation + framing (prose/reports/forms).
      - ``code`` — preserve the file tree, generate the wiring (apps/modules);
        NOT a concat-into-one-blob. (Part B / code-assembly.)
      - ``media`` / ``data`` — a render/merge TOOL (Part B seams).

    Unknown strategy → an error result so the caller fails closed.
    """
    fn = _STRATEGIES.get(strategy)
    if fn is None:
        return AssemblyResult(
            content="",
            missing=[str(u) for u in manifest.get("units", [])],
            errors=[f"unknown assembly strategy {strategy!r}"],
        )
    return fn(manifest, artifacts_root)


def _assemble_document(manifest: dict, artifacts_root: Path) -> AssemblyResult:
    """The ``document`` strategy: concatenate the manifest's unit files (read from
    disk) into one body.

    Order is the manifest's ``units`` order — data, not opinion. Missing or
    unsafe units are recorded (never fabricated, never silently dropped);
    assembly proceeds best-effort with whatever resolved so the caller can
    ship-with-blocker. ``title_page`` leads, ``trailer`` trails, both
    optional; blocks are joined by ``separator``.
    """
    title_page = manifest.get("title_page")
    trailer = manifest.get("trailer")
    separator = manifest.get("separator")
    if not isinstance(separator, str):
        separator = _DEFAULT_SEPARATOR
    title_page = title_page if isinstance(title_page, str) else ""
    trailer = trailer if isinstance(trailer, str) else ""

    blocks: list[str] = []
    used: list[str] = []
    missing: list[str] = []
    errors: list[str] = []

    if title_page.strip():
        blocks.append(title_page.rstrip("\n"))

    total = len(title_page)
    for name in manifest["units"]:
        path = _safe_unit_path(name, artifacts_root)
        if path is None:
            errors.append(f"unsafe or out-of-root unit path: {name!r}")
            missing.append(name)
            continue
        if not path.is_file():
            missing.append(name)
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            errors.append(f"stat failed for {name!r}: {exc}")
            missing.append(name)
            continue
        if size > _MAX_UNIT_BYTES:
            errors.append(
                f"unit {name!r} is {size} bytes (> {_MAX_UNIT_BYTES} cap); skipped"
            )
            missing.append(name)
            continue
        if total + size > _MAX_TOTAL_BYTES:
            errors.append(
                f"total assembled size would exceed {_MAX_TOTAL_BYTES} bytes; "
                f"stopped before {name!r}"
            )
            break
        try:
            body = path.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"read failed for {name!r}: {exc}")
            missing.append(name)
            continue
        blocks.append(body.strip("\n"))
        used.append(name)
        total += size

    if trailer.strip():
        blocks.append(trailer.strip("\n"))

    content = separator.join(blocks)
    return AssemblyResult(
        content=content, units_used=used, missing=missing, errors=errors,
    )


def _assemble_code(manifest: dict, artifacts_root: Path) -> AssemblyResult:
    """The ``code`` strategy: a multi-file deliverable is its FILE TREE plus
    generated wiring — NOT a concat-into-one-blob (you don't ``cat`` sources into
    one file). The unit files STAY SEPARATE on disk; the assembled output_path is
    a generated index/manifest (a README) listing the files + entry point, so the
    product is usable as one whole.

    Unit bodies are never read (the index is small — no output-token / QC-budget
    pressure). Presence/safety is still checked; missing/unsafe units are recorded
    (the assembly is then incomplete and not eligible for the cheap QC pass).
    """
    title = manifest.get("title_page")
    title = title.strip() if isinstance(title, str) and title.strip() else "Project"
    entrypoint = manifest.get("entrypoint")

    used: list[str] = []
    missing: list[str] = []
    errors: list[str] = []
    for name in manifest["units"]:
        path = _safe_unit_path(name, artifacts_root)
        if path is None:
            errors.append(f"unsafe or out-of-root unit path: {name!r}")
            missing.append(name)
            continue
        if not path.is_file():
            missing.append(name)
            continue
        used.append(name)

    lines = [f"# {title}", "", f"{len(used)} file(s):", ""]
    lines += [f"- `{_norm(u)}`" for u in used]
    if isinstance(entrypoint, str) and entrypoint.strip():
        lines += ["", f"Entry point: `{entrypoint.strip()}`"]
    content = "\n".join(lines) + "\n"
    return AssemblyResult(
        content=content, units_used=used, missing=missing, errors=errors,
    )


def _norm(name: str) -> str:
    return str(name).strip().lstrip("./")


def _assemble_data(manifest: dict, artifacts_root: Path) -> AssemblyResult:
    """The ``data`` strategy: a mechanical MERGE/FOLD of homogeneous structured
    units into one dataset — JSON arrays concatenated, CSV rows stacked under one
    header. Pure engine, no LLM. ``format`` (json|csv) is explicit or inferred
    from the first unit's extension; ``dedupe: true`` drops exact-equal records.

    (Cross-schema joins / complex folds belong to a tool — Part B4. A unit that
    fails to parse is recorded as an error, making the assembly incomplete and
    ineligible for the cheap QC pass.)
    """
    dedupe = bool(manifest.get("dedupe"))
    fmt = str(manifest.get("format") or "").lower()

    resolved: list[tuple[str, str]] = []
    missing: list[str] = []
    errors: list[str] = []
    total = 0
    for name in manifest["units"]:
        path = _safe_unit_path(name, artifacts_root)
        if path is None:
            errors.append(f"unsafe or out-of-root unit path: {name!r}")
            missing.append(name)
            continue
        if not path.is_file():
            missing.append(name)
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            errors.append(f"stat failed for {name!r}: {exc}")
            missing.append(name)
            continue
        if size > _MAX_UNIT_BYTES:
            errors.append(f"unit {name!r} is {size} bytes (> cap); skipped")
            missing.append(name)
            continue
        if total + size > _MAX_TOTAL_BYTES:
            errors.append(f"total merged size would exceed {_MAX_TOTAL_BYTES} bytes")
            break
        try:
            resolved.append((name, path.read_text()))
            total += size
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"read failed for {name!r}: {exc}")
            missing.append(name)

    if not fmt:
        first = resolved[0][0].lower() if resolved else ""
        fmt = "csv" if first.endswith(".csv") else "json"

    used = [n for n, _ in resolved]
    if fmt == "csv":
        content, merge_errs = _merge_csv([t for _, t in resolved], dedupe)
    else:
        content, merge_errs = _merge_json(resolved, dedupe)
    return AssemblyResult(
        content=content, units_used=used, missing=missing, errors=errors + merge_errs,
    )


def _merge_json(items: list[tuple[str, str]], dedupe: bool) -> tuple[str, list[str]]:
    merged: list = []
    errors: list[str] = []
    for name, text in items:
        try:
            obj = json.loads(text)
        except ValueError as exc:
            errors.append(f"{name}: invalid JSON ({exc})")
            continue
        merged.extend(obj if isinstance(obj, list) else [obj])
    if dedupe:
        seen: set[str] = set()
        out: list = []
        for el in merged:
            key = json.dumps(el, sort_keys=True)
            if key not in seen:
                seen.add(key)
                out.append(el)
        merged = out
    return json.dumps(merged, indent=2) + "\n", errors


def _merge_csv(texts: list[str], dedupe: bool) -> tuple[str, list[str]]:
    header: str | None = None
    rows: list[str] = []
    for text in texts:
        lines = [ln for ln in text.splitlines() if ln != ""]
        if not lines:
            continue
        if header is None:
            header = lines[0]
        rows.extend(lines[1:] if lines[0] == header else lines)
    if dedupe:
        seen: set[str] = set()
        out: list[str] = []
        for r in rows:
            if r not in seen:
                seen.add(r)
                out.append(r)
        rows = out
    if header is None:
        return "", []
    return "\n".join([header] + rows) + "\n", []


#: Family → mechanical-join function. ``document`` (text concat), ``code`` (file
#: tree + generated index), and ``data`` (structured merge/fold) are live; ``media``
#: lands as a seam (needs a render tool — Part B4). The dispatch is what makes
#: assembly product-agnostic — the assembler SKILL selects the strategy; the ENGINE
#: owns the join; unit bytes never round-trip through the model.
_STRATEGIES: dict = {
    "document": _assemble_document,
    "code": _assemble_code,
    "data": _assemble_data,
}
