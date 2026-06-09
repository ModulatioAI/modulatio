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

import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
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
    #: For BINARY families (media): the engine-composited file the strategy wrote
    #: to disk (inside artifacts_root). When set, ``content`` is a human-readable
    #: receipt (not the deliverable), and the caller moves this file into the
    #: task's output_path + checksums ITS bytes — text strategies leave it None.
    output_file: "Path | None" = None


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
    #: For BINARY (media) assemblies: the engine-composited file on disk. When set,
    #: the caller moves it onto the deliverable path and ``final_checksum`` is the
    #: hash of ITS bytes (not of ``content``). None for text strategies.
    output_file: "Path | None" = None
    #: #101 Part 0: the engine-extracted structural digest of this deliverable (the
    #: verifier's eyes), attached at assembly time. None until Part 0 wiring runs.
    digest: "DeliverableDigest | None" = None


@dataclass
class DeliverableDigest:
    """Engine-extracted, MODEL-READABLE structure of an assembled deliverable — the
    verifier's EYES (#101 Part 0). Computed at assembly time so the smart layer can
    judge the WHOLE without reading binary bytes it cannot (the HRWT Leader-verify
    was handed ``"(could not read: …)"`` and shipped ``on_the_fence``).

    PRODUCT-AGNOSTIC by design. The engine defines only the CONTRACT — how many
    PARTS, a ``label`` + ``size`` per part, which structural elements are present, an
    optional whole-deliverable size — and each artifact FAMILY fills it with
    domain-appropriate facts (a document's parts are sections sized in words; a
    dataset's are tables sized in rows; a codebase's are files sized in lines). The
    per-``artifact_kind`` STANDARDS say what the numbers must satisfy. Nothing here
    assumes "document" — baking one output class in is the recurring failure mode."""

    #: the artifact family that produced this digest ("document", "data", "code", …).
    kind: str
    #: number of assembled parts (units).
    part_count: int
    #: ordered, one per part. Each is a small family-defined dict that ALWAYS carries
    #: ``label`` (str) and ``size`` (int); a family may add its own extra keys.
    parts: list[dict] = field(default_factory=list)
    #: what each part's ``size`` counts — "words" | "rows" | "lines" | "bytes" | …
    part_size_unit: str = ""
    #: structural elements ACTUALLY present, family-defined (e.g. {"title": True,
    #: "toc": True} for a document; {"header_row": True} for a dataset).
    structure: dict = field(default_factory=dict)
    #: a whole-deliverable size measure + its unit, family-defined (pages for a
    #: paginated doc, total rows for data, total files for code). None when N/A.
    whole_size: "int | None" = None
    whole_size_unit: "str | None" = None
    #: relative path to the readable text twin of the bound product, when persisted.
    text_twin_path: "str | None" = None


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
    # Nemo B4 #5: reject C0 control chars (esp. \r \n \0). A legitimate artifact
    # filename never contains them, and a newline in a unit name could inject a
    # `file '...'` directive into ffmpeg's line-oriented concat list (the media
    # join) — belt-and-suspenders beyond the single-quote escaping there.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in stripped):
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


# ── #101 Part 0: the deliverable digest (give the verifier eyes) ──────────────

def _first_heading(body: str) -> str:
    """A unit's display heading: the first non-empty line, stripped of leading
    markdown ``#`` and surrounding whitespace. ``""`` for an empty body."""
    for line in body.splitlines():
        s = line.strip()
        if s:
            return s.lstrip("#").strip()
    return ""


def _pdf_page_count(path: "Path | None") -> "int | None":
    """Page count of a rendered PDF via ``pdfinfo`` (engine-owned, fail-open). Returns
    None when the file isn't a readable PDF, ``pdfinfo`` is absent, or the probe
    fails — never raises (the digest is best-effort observability, not a gate)."""
    if path is None or path.suffix.lower() != ".pdf" or not path.is_file():
        return None
    tool = resolve_tool("pdfinfo")
    if tool is None:
        return None
    try:
        proc = subprocess.run(
            [tool, str(path)], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        if line.lower().startswith("pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _generic_digest(
    manifest: dict,
    units_used: "list[str]",
    artifacts_root: Path,
    *,
    kind: str = "generic",
    output_file: "Path | None" = None,
    text_twin_path: "str | None" = None,
) -> DeliverableDigest:
    """Family-NEUTRAL digest: any deliverable is N parts of some BYTE size. The
    default until a family grows a richer extractor — it reads NO domain meaning from
    the bytes, so it is correct for code / data / media / anything at all."""
    parts: list[dict] = []
    for name in units_used:
        path = _safe_unit_path(name, artifacts_root)
        size = path.stat().st_size if (path is not None and path.is_file()) else 0
        parts.append({"label": str(name), "size": size})
    return DeliverableDigest(
        kind=kind, part_count=len(units_used), parts=parts,
        part_size_unit="bytes", text_twin_path=text_twin_path,
    )


def _document_digest(
    manifest: dict,
    units_used: "list[str]",
    artifacts_root: Path,
    *,
    output_file: "Path | None" = None,
    text_twin_path: "str | None" = None,
) -> DeliverableDigest:
    """The ``document`` family's digest: parts are sections (heading ``label`` + word
    ``size``), ``structure`` is title/TOC presence, ``whole_size`` is the PDF page
    count. ALL the document-domain meaning lives HERE, in the family extractor — never
    in the generic contract. Best-effort + fail-open: an unreadable/missing unit
    contributes ``{"label": "", "size": 0}`` and never raises."""
    parts: list[dict] = []
    for name in units_used:
        path = _safe_unit_path(name, artifacts_root)
        if path is None or not path.is_file():
            parts.append({"label": "", "size": 0})
            continue
        try:
            body = path.read_text()
        except (OSError, UnicodeDecodeError):
            parts.append({"label": "", "size": 0})
            continue
        parts.append({"label": _first_heading(body), "size": len(body.split())})
    title_page = manifest.get("title_page")
    structure = {
        "title": bool(isinstance(title_page, str) and title_page.strip()),
        "toc": bool(manifest.get("toc")) or any(
            "table of contents" in p["label"].lower() or p["label"].lower() == "contents"
            for p in parts
        ),
    }
    return DeliverableDigest(
        kind="document", part_count=len(units_used), parts=parts,
        part_size_unit="words", structure=structure,
        whole_size=_pdf_page_count(output_file), whole_size_unit="pages",
        text_twin_path=text_twin_path,
    )


#: Per-family digest extractors (mirrors the ``_STRATEGIES`` assembly table). A family
#: without a rich extractor falls back to the family-neutral byte digest.
_DIGEST_BUILDERS: dict = {"document": _document_digest}


def build_deliverable_digest(
    manifest: dict,
    units_used: "list[str]",
    artifacts_root: Path,
    *,
    strategy: str = "document",
    output_file: "Path | None" = None,
    text_twin_path: "str | None" = None,
) -> DeliverableDigest:
    """Build the deliverable digest for the named STRATEGY/family (#101 Part 0) —
    PRODUCT-AGNOSTIC dispatch (mirrors ``assemble``'s strategy table). ``document``
    has a rich extractor; every other family falls back to the family-neutral byte
    digest until it grows its own. ``units_used`` is the engine's authoritative
    ordered set, not the producer's manifest claim. Fail-open throughout."""
    builder = _DIGEST_BUILDERS.get(strategy)
    if builder is None:
        return _generic_digest(
            manifest, units_used, artifacts_root, kind=strategy,
            output_file=output_file, text_twin_path=text_twin_path,
        )
    return builder(
        manifest, units_used, artifacts_root,
        output_file=output_file, text_twin_path=text_twin_path,
    )


def write_text_twin(content: str, artifacts_root: Path, name: str) -> str:
    """Persist the readable markdown TWIN of a bound (binary) deliverable under
    ``artifacts_root/.twins/`` so the verifier has eyes on bytes it cannot read (#101
    Part 0). Engine-owned; ``name`` is sanitized (a task id — never a producer path).
    Returns the path RELATIVE to ``artifacts_root``."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("_") or "twin"
    twins = artifacts_root / ".twins"
    twins.mkdir(parents=True, exist_ok=True)
    out = twins / f"{safe}.md"
    out.write_text(content)
    return str(out.relative_to(artifacts_root))


def assemble(
    manifest: dict, artifacts_root: Path, strategy: str = "document",
    render_format: "str | None" = None,
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
    if strategy == "document":
        return _assemble_document(
            manifest, artifacts_root, render_format=render_format
        )
    return fn(manifest, artifacts_root)


def _assemble_document(
    manifest: dict, artifacts_root: Path, render_format: "str | None" = None,
) -> AssemblyResult:
    """The ``document`` strategy: concatenate the manifest's unit files (read from
    disk) into one body, then OPTIONALLY render that body into a declared binary
    document format (``render_format`` — docx/odt/rtf/epub/pdf/…).

    Order is the manifest's ``units`` order — data, not opinion. Missing or
    unsafe units are recorded (never fabricated, never silently dropped);
    assembly proceeds best-effort with whatever resolved so the caller can
    ship-with-blocker. ``title_page`` leads, ``trailer`` trails, both
    optional; blocks are joined by ``separator``.

    ``render_format`` is the deliverable's DECLARED format, not an assumption —
    Modulatio is artifact-agnostic, so the engine renders whatever format the
    deliverable asked for and imposes none when none is declared (the body stays
    text). Render is fail-closed: a missing toolchain keeps the real text and
    flags the binary as unrendered, never fabricates a binary.
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

    # Producer-authored framing (title/trailer/separator) is UNTRUSTED and counts
    # toward the runaway-output cap — a huge title_page/trailer must not slip past
    # _MAX_TOTAL_BYTES the way it did before (unit st_size alone was counted).
    framing_bytes = len(title_page.encode()) + len(trailer.encode())
    if framing_bytes > _MAX_TOTAL_BYTES:
        errors.append("framing (title_page/trailer) exceeds total size cap")
        title_page = trailer = ""
        framing_bytes = 0

    if title_page.strip():
        blocks.append(title_page.rstrip("\n"))

    sep_bytes = len(separator.encode())
    total = framing_bytes
    over_cap = False
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
        # Count the SEPARATOR that will precede this block, not just the body —
        # a huge separator × N blocks must not slip past the cap (Nemo hull #1).
        added = size + (sep_bytes if blocks else 0)
        if total + added > _MAX_TOTAL_BYTES:
            errors.append(
                f"total assembled size would exceed {_MAX_TOTAL_BYTES} bytes; "
                f"stopped before {name!r}"
            )
            over_cap = True
            break
        try:
            body = path.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"read failed for {name!r}: {exc}")
            missing.append(name)
            continue
        blocks.append(body.strip("\n"))
        used.append(name)
        total += added

    if trailer.strip():
        blocks.append(trailer.strip("\n"))

    content = separator.join(blocks)
    # Hard belt: NEVER return over-cap content for writing (Nemo hull #1 — the
    # old "final check" only logged an error but still returned the bytes). An
    # over-cap assembly is incomplete → fail-closed to a full review.
    if over_cap or len(content.encode()) > _MAX_TOTAL_BYTES:
        if not over_cap:
            errors.append(f"assembled output exceeds {_MAX_TOTAL_BYTES} bytes")
        content = ""
    result = AssemblyResult(
        content=content, units_used=used, missing=missing, errors=errors,
    )
    # P4: render the assembled markdown into the deliverable's DECLARED binary
    # document format, via an engine-owned tool. Fail-closed — a missing toolchain
    # keeps the real text (an openable .md) and flags the binary as unrendered;
    # never a fabricated binary. No render_format → text stands (no binary imposed).
    if content and render_format:
        try:
            out_file, _msg = render_document(content, render_format, artifacts_root)
            result.output_file = out_file
        except (_DocToolError, _MediaToolError) as exc:
            # Catch BOTH: a missing/failed render tool raises _DocToolError; the
            # output-size cap (_check_output_size) raises _MediaToolError. Either way
            # fail CLOSED — keep the real text, flag the binary as unrendered, never
            # let it escape and never fabricate (Nemo hull #8).
            result.errors.append(
                f"binary render unavailable ({render_format}); kept text — {exc}"
            )
    return result


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
        content, merge_errs = _merge_csv(resolved, dedupe)
    else:
        content, merge_errs = _merge_json(resolved, dedupe)
    return AssemblyResult(
        content=content, units_used=used, missing=missing, errors=errors + merge_errs,
    )


def _capped(content: str, errors: list[str], what: str) -> tuple[str, list[str]]:
    """Enforce the final-output cap on a merged dataset (Nemo hull #2 —
    serialization/quoting can expand a near-cap input past the cap). Over → fail
    closed (empty content + error → incomplete → full review)."""
    if len(content.encode()) > _MAX_TOTAL_BYTES:
        return "", errors + [f"merged {what} exceeds {_MAX_TOTAL_BYTES} bytes"]
    return content, errors


def _dedupe_key(text: str) -> str:
    """Bounded dedupe key — a short hash, so the `seen` set can't grow to the
    full serialized size of a huge dataset (Nemo hull #2 memory bound)."""
    return hashlib.sha256(text.encode()).hexdigest()


def _merge_json(items: list[tuple[str, str]], dedupe: bool) -> tuple[str, list[str]]:
    merged: list = []
    errors: list[str] = []
    for name, text in items:
        # Untrusted unit content: ValueError = bad JSON; RecursionError =
        # deeply-nested JSON (NOT a ValueError). Both caught → unit recorded as an
        # error (→ incomplete → full review), never escaping the merge.
        try:
            obj = json.loads(text)
        except (ValueError, RecursionError) as exc:
            errors.append(f"{name}: invalid JSON ({type(exc).__name__})")
            continue
        merged.extend(obj if isinstance(obj, list) else [obj])
    if dedupe:
        seen: set[str] = set()
        out: list = []
        for el in merged:
            try:
                key = _dedupe_key(json.dumps(el, sort_keys=True))
            except (ValueError, RecursionError):
                out.append(el)  # un-dedupable element; the final serialize rules
                continue
            if key not in seen:
                seen.add(key)
                out.append(el)
        merged = out
    try:
        content = json.dumps(merged, indent=2) + "\n"
    except (ValueError, RecursionError) as exc:
        return "", errors + [f"merged JSON not serializable ({type(exc).__name__})"]
    return _capped(content, errors, "JSON")


def _merge_csv(items: list[tuple[str, str]], dedupe: bool) -> tuple[str, list[str]]:
    """Stack CSV units under ONE header. ``strict=True`` so malformed quoting is a
    real ``csv.Error`` (not silently normalized — Nemo hull #3). Header mismatch
    AND row-arity mismatch are recorded errors → the assembly is incomplete
    (fail-closed), never silently merging mismatched/garbage schemas."""
    csv.field_size_limit(_MAX_UNIT_BYTES)
    header: list[str] | None = None
    rows: list[list[str]] = []
    errors: list[str] = []
    for name, text in items:
        try:
            parsed = [r for r in csv.reader(io.StringIO(text), strict=True) if r]
        except (csv.Error, ValueError) as exc:
            errors.append(f"{name}: invalid CSV ({exc})")
            continue
        if not parsed:
            continue
        if header is None:
            header = parsed[0]
        elif parsed[0] != header:
            errors.append(f"{name}: CSV header mismatch (expected {header})")
            continue
        width = len(header)
        for row in parsed[1:]:
            if len(row) != width:
                errors.append(f"{name}: CSV row arity {len(row)} != header {width}")
                continue
            rows.append(row)
    if dedupe:
        seen: set[str] = set()
        out: list[list[str]] = []
        for r in rows:
            key = _dedupe_key("\x00".join(r))
            if key not in seen:
                seen.add(key)
                out.append(r)
        rows = out
    if header is None:
        return "", errors
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return _capped(buf.getvalue(), errors, "CSV")


#: External-compositor wall-clock ceiling. A media join that can't finish in this
#: many seconds is treated as failed (fail closed → normal review).
_MEDIA_JOIN_TIMEOUT_SECONDS = 120

_VIDEO_EXTS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"})
_AUDIO_EXTS = frozenset({".mp3", ".wav", ".flac", ".aac", ".ogg", ".oga", ".m4a"})
_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp"})


class _MediaToolError(Exception):
    """A media compositor was absent, timed out, or failed. Caught by
    ``_assemble_media`` → fail closed (the assembly routes to a normal review;
    we never write a half-composited or wrong-kind binary)."""


def _media_kind(manifest: dict, resolved: "list[tuple[str, Path]]") -> str:
    """The media family to join by: explicit ``media_kind``, else inferred from
    the units' extensions (homogeneous video/audio/image), else ``bundle``."""
    declared = str(manifest.get("media_kind") or "").strip().lower()
    if declared in ("video", "audio", "image", "bundle"):
        return declared
    exts = {p.suffix.lower() for _n, p in resolved}
    if exts and exts <= _VIDEO_EXTS:
        return "video"
    if exts and exts <= _AUDIO_EXTS:
        return "audio"
    if exts and exts <= _IMAGE_EXTS:
        return "image"
    return "bundle"


#: Extra directories to search for an engine tool BEYOND ``PATH`` — the common
#: non-PATH installs (a user's ~/bin, pipx/local, Homebrew, /opt, snap). The HRWT
#: render failed only because pandoc lived in ``~/bin``, off the launching process's
#: PATH; the engine should find a tool wherever it is, not only when the shell that
#: started it happened to export the right PATH.
_TOOL_SEARCH_DIRS: tuple[str, ...] = (
    "~/bin", "~/.local/bin",
    "/usr/local/bin", "/usr/bin", "/bin",
    "/opt/bin", "/opt/local/bin", "/opt/homebrew/bin",
    "/snap/bin",
)


def _usable_abs(p: "Path") -> bool:
    """True iff ``p`` is an ABSOLUTE path to an executable regular file."""
    return p.is_absolute() and p.is_file() and os.access(str(p), os.X_OK)


def resolve_tool(name: str) -> "str | None":
    """Resolve an external engine tool to an ABSOLUTE path, robust to ``PATH``.

    Order (Nemo hull #6/#7 — the security contract is a real absolute,
    PATH-independent invocation):

    1. operator override ``MODULATIO_<NAME>_PATH`` — must be an ABSOLUTE path to an
       executable. A *set-but-unusable* override (relative, missing, non-exec) is a
       HARD STOP (returns None), never a silent fall-through to a different binary —
       "use THIS" must not quietly become "use whatever's on PATH."
    2. the curated absolute system dirs (:data:`_TOOL_SEARCH_DIRS`) — checked BEFORE
       ``PATH`` so a contaminated or relative ``PATH`` entry cannot SHADOW
       ``/usr/bin`` etc.
    3. ``PATH`` last (``shutil.which``), and only if it yields an ABSOLUTE path
       (relative / cwd ``PATH`` components are never trusted for an engine tool).

    Returns ``None`` when genuinely unresolvable. The tool runs as a plain
    engine-owned subprocess invoked by THIS absolute path — never inside the
    producer sandbox."""
    override = os.environ.get(f"MODULATIO_{name.upper().replace('-', '_')}_PATH")
    if override:
        p = Path(override)
        return str(p) if _usable_abs(p) else None  # set ⇒ honor or fail; no fallthrough
    for d in _TOOL_SEARCH_DIRS:
        cand = (Path(d).expanduser() / name).resolve()
        if _usable_abs(cand):
            return str(cand)
    found = shutil.which(name)
    if found:
        fp = Path(found)
        if fp.is_absolute():
            fp = fp.resolve()
            if _usable_abs(fp):
                return str(fp)
    return None


def _run_media_join(argv: "list[str]", *, tool: str) -> None:
    """Run an external compositor, fail-closed on absent/timeout/non-zero. The
    engine owns this subprocess (not the producer sandbox); argv is built from
    engine-validated paths only, never producer-supplied flags."""
    resolved = resolve_tool(argv[0])
    if resolved is None:
        raise _MediaToolError(
            f"media assembly needs {tool} — not installed "
            f"(install it to enable {tool}-based media joins)"
        )
    argv = [resolved, *argv[1:]]  # invoke by absolute path — PATH-independent
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=_MEDIA_JOIN_TIMEOUT_SECONDS, check=False,
        )
    except FileNotFoundError as exc:
        raise _MediaToolError(f"media assembly needs {tool} — not installed ({exc})")
    except subprocess.TimeoutExpired:
        raise _MediaToolError(
            f"media assembly: {tool} timed out after {_MEDIA_JOIN_TIMEOUT_SECONDS}s"
        )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        why = tail[-1] if tail else f"{tool} exit {proc.returncode}"
        raise _MediaToolError(f"media assembly: {tool} failed — {why}")


_DOC_RENDER_TIMEOUT_SECONDS = 180.0
#: Document formats pandoc writes DIRECTLY from markdown by output extension.
#: Artifact-agnostic: the engine renders whatever format the deliverable declares;
#: this set is "what the tool can do," not "what a document is assumed to be." PDF
#: is handled separately (a docx→libreoffice bridge) to avoid a LaTeX dependency.
_PANDOC_DIRECT_FORMATS: frozenset[str] = frozenset(
    {"docx", "odt", "rtf", "html", "epub", "tex"}
)


class _DocToolError(Exception):
    """A document-render tool was absent or failed — the caller fails closed to
    the assembled text (keeps the real content; flags the binary as unrendered)."""


def _run_doc_tool(argv: "list[str]", *, tool: str) -> None:
    """Run a document-render tool, fail-closed on absent/timeout/non-zero. Engine-
    owned (not the producer sandbox); argv is built from engine-validated paths.
    The tool is resolved to an ABSOLUTE path (robust to PATH) so the render works
    however the process was launched — the HRWT pandoc-in-~/bin failure."""
    resolved = resolve_tool(argv[0])
    if resolved is None:
        raise _DocToolError(
            f"{tool} is not installed or not found "
            f"(set MODULATIO_{argv[0].upper().replace('-', '_')}_PATH to its path)"
        )
    argv = [resolved, *argv[1:]]  # invoke by absolute path — PATH-independent
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=_DOC_RENDER_TIMEOUT_SECONDS, check=False,
        )
    except FileNotFoundError as exc:
        raise _DocToolError(f"{tool} is not installed ({exc})")
    except subprocess.TimeoutExpired:
        raise _DocToolError(
            f"{tool} timed out after {_DOC_RENDER_TIMEOUT_SECONDS}s"
        )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        why = tail[-1] if tail else f"exit {proc.returncode}"
        raise _DocToolError(f"{tool} failed — {why}")


def render_document(content: str, fmt: str, artifacts_root: Path) -> "tuple[Path, str]":
    """Render assembled markdown ``content`` into the DECLARED binary document
    format ``fmt``, engine-owned subprocess, fail-closed.

    Modulatio is artifact-agnostic: ``fmt`` is whatever the deliverable declared
    (docx/odt/rtf/epub/pdf/…), never an assumed default. ``md→<fmt>`` via pandoc;
    ``pdf`` via a pandoc→docx→libreoffice bridge (no LaTeX dependency). Returns the
    rendered file path inside ``artifacts_root`` (the engine moves it onto the
    deliverable path). Raises :class:`_DocToolError` when the toolchain is
    unavailable, so the caller keeps the real text and flags the binary as
    unrendered — it NEVER fabricates a binary (the HRWT text-named-.pdf failure)."""
    fmt = (fmt or "").lower().lstrip(".")
    src = _media_out(artifacts_root, ".md")
    src.write_text(content)
    try:
        if fmt in _PANDOC_DIRECT_FORMATS:
            out = _media_out(artifacts_root, f".{fmt}")
            try:
                _run_doc_tool(["pandoc", str(src), "-o", str(out)], tool="pandoc")
                _check_output_size(out)
            except (_DocToolError, _MediaToolError):
                # Fail-closed hygiene (Nemo hull #9): a failed/oversized render must
                # not leave a partial hidden output behind to pollute artifact scans.
                out.unlink(missing_ok=True)
                raise
            return out, f"rendered .{fmt} via pandoc"
        if fmt == "pdf":
            docx_tmp = _media_out(artifacts_root, ".docx")
            _run_doc_tool(["pandoc", str(src), "-o", str(docx_tmp)], tool="pandoc")
            try:
                _run_doc_tool(
                    ["soffice", "--headless", "--convert-to", "pdf",
                     "--outdir", str(artifacts_root), str(docx_tmp)],
                    tool="libreoffice",
                )
                pdf_out = docx_tmp.with_suffix(".pdf")
                if not pdf_out.is_file():
                    raise _DocToolError("libreoffice produced no PDF")
                _check_output_size(pdf_out)
                return pdf_out, "rendered .pdf via pandoc+libreoffice"
            finally:
                docx_tmp.unlink(missing_ok=True)
        raise _DocToolError(f"unsupported document render format {fmt!r}")
    finally:
        src.unlink(missing_ok=True)


def _media_out(artifacts_root: Path, suffix: str) -> Path:
    """A fresh temp output path INSIDE artifacts_root (so the engine can move it
    onto the deliverable path, same filesystem; producer-untrusted names never
    touch it)."""
    fd, name = tempfile.mkstemp(prefix=".assembly_media_", suffix=suffix, dir=str(artifacts_root))
    os.close(fd)
    return Path(name)


def _check_output_size(out: Path) -> None:
    """A composited binary over the total cap is discarded + fails closed."""
    if out.stat().st_size > _MAX_TOTAL_BYTES:
        out.unlink(missing_ok=True)
        raise _MediaToolError(
            f"media assembly: composited output exceeds {_MAX_TOTAL_BYTES} bytes"
        )


def _join_bundle(resolved: "list[tuple[str, Path]]", artifacts_root: Path) -> "tuple[Path, str]":
    """Heterogeneous units → one ZIP. Stdlib ``zipfile`` — no external binary, so
    bundle never fails closed on a missing tool."""
    out = _media_out(artifacts_root, ".zip")
    try:
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, path in resolved:
                zf.write(path, arcname=name)
    except OSError as exc:
        out.unlink(missing_ok=True)
        raise _MediaToolError(f"media assembly: bundle write failed — {exc}")
    _check_output_size(out)
    return out, f"bundled {len(resolved)} unit(s) into a zip archive"


def _join_av(resolved: "list[tuple[str, Path]]", artifacts_root: Path, kind: str) -> "tuple[Path, str]":
    """Video/audio units → one stream via ffmpeg's concat demuxer (stream copy,
    no re-encode). Mismatched codecs make ffmpeg fail → fail closed (re-encode is
    out of scope)."""
    suffix = resolved[0][1].suffix.lower() or (".mp4" if kind == "video" else ".m4a")
    out = _media_out(artifacts_root, suffix)
    with tempfile.TemporaryDirectory() as td:
        listfile = Path(td) / "concat.txt"
        # ffmpeg concat list: each line `file '<abs path>'`; single-quotes in a
        # path are escaped per ffmpeg's rule ('\'').
        lines = [f"file '{str(p).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'"
                 for _n, p in resolved]
        listfile.write_text("\n".join(lines) + "\n")
        try:
            _run_media_join(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(listfile), "-c", "copy", str(out)],
                tool="ffmpeg",
            )
        except _MediaToolError:
            out.unlink(missing_ok=True)
            raise
    _check_output_size(out)
    return out, f"concatenated {len(resolved)} {kind} unit(s) via ffmpeg (stream copy)"


def _join_image(resolved: "list[tuple[str, Path]]", artifacts_root: Path,
                manifest: dict) -> "tuple[Path, str]":
    """Image units → one composite via ImageMagick. ``layout`` = ``append`` (a
    vertical strip via ``convert -append``) or ``montage`` (a grid, default).
    ImageMagick absent → fail closed."""
    out = _media_out(artifacts_root, resolved[0][1].suffix.lower() or ".png")
    paths = [str(p) for _n, p in resolved]
    layout = str(manifest.get("layout") or "montage").strip().lower()
    # ImageMagick 7 prefers `magick ...`; 6 ships `convert`/`montage`. Pick what's
    # present so a v7-only box (no legacy shims) still works. Robust discovery
    # (resolve_tool) so a ~/bin / Homebrew install is found regardless of PATH.
    has_magick = resolve_tool("magick") is not None
    try:
        if layout == "append":
            argv = (["magick"] if has_magick else []) + ["convert", *paths, "-append", str(out)]
            _run_media_join(argv, tool="ImageMagick")
        else:
            argv = (["magick", "montage"] if has_magick else ["montage"]) + [
                *paths, "-tile", "x1", "-geometry", "+2+2", str(out)
            ]
            _run_media_join(argv, tool="ImageMagick")
    except _MediaToolError:
        out.unlink(missing_ok=True)
        raise
    _check_output_size(out)
    return out, f"composited {len(resolved)} image(s) via ImageMagick ({layout})"


def _assemble_media(manifest: dict, artifacts_root: Path) -> AssemblyResult:
    """The ``media`` strategy (image/audio/video/bundle): join binary units with a
    LOCAL compositor — ``bundle`` via stdlib zip, ``video``/``audio`` via ffmpeg
    concat, ``image`` via ImageMagick. The engine owns the subprocess and the
    output stays in the vault; unit bytes never round-trip through the model.

    A missing external tool (ffmpeg/ImageMagick) fails CLOSED with a clear note —
    the assembly routes to a normal review rather than shipping a half- or
    wrong-composited binary (mirrors the renderer-degradation discipline). The
    composited file is returned via ``output_file`` (the engine moves it onto the
    deliverable + checksums its bytes); ``content`` is a human-readable receipt.
    """
    units = manifest.get("units", [])
    resolved: list[tuple[str, Path]] = []
    missing: list[str] = []
    errors: list[str] = []
    for name in units:
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
            errors.append(f"unit {name!r} is {size} bytes (> {_MAX_UNIT_BYTES} cap); skipped")
            missing.append(name)
            continue
        resolved.append((name, path))

    if not resolved:
        return AssemblyResult(
            content="", missing=missing,
            errors=errors or ["media assembly: no resolvable units"],
        )

    kind = _media_kind(manifest, resolved)
    try:
        if kind == "bundle":
            out_file, receipt = _join_bundle(resolved, artifacts_root)
        elif kind in ("video", "audio"):
            out_file, receipt = _join_av(resolved, artifacts_root, kind)
        elif kind == "image":
            out_file, receipt = _join_image(resolved, artifacts_root, manifest)
        else:  # pragma: no cover - _media_kind only yields the four above
            return AssemblyResult(
                content="", missing=[n for n, _ in resolved] + missing,
                errors=errors + [f"media assembly: unknown media_kind {kind!r}"],
            )
    except _MediaToolError as exc:
        # Fail closed: the binary the producer expected does NOT get written.
        return AssemblyResult(
            content="", units_used=[],
            missing=[n for n, _ in resolved] + missing,
            errors=errors + [str(exc)],
        )

    return AssemblyResult(
        content=f"media assembly ({kind}): {receipt}",
        units_used=[n for n, _ in resolved],
        missing=missing,
        errors=errors,
        output_file=out_file,
    )


#: Family → mechanical-join function. ``document`` (text concat), ``code`` (file
#: tree + generated index), and ``data`` (structured merge/fold) are live; ``media``
#: is a registered SEAM that fails closed until its render tool lands (Part B4). The
#: dispatch is what makes assembly product-agnostic — the assembler SKILL selects
#: the strategy; the ENGINE owns the join; unit bytes never round-trip through the
#: model.
_STRATEGIES: dict = {
    "document": _assemble_document,
    "code": _assemble_code,
    "data": _assemble_data,
    "media": _assemble_media,
}
