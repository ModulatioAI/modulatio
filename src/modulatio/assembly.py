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


def assemble(manifest: dict, artifacts_root: Path) -> AssemblyResult:
    """Concatenate the manifest's unit files (read from disk) into one body.

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
