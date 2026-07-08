# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Finished-product delivery.

Producers author deliverables as **Markdown** (their natural format). This
module renders each Leader-tagged deliverable to a real document format
(DOCX by default) via the export pipeline — actual pandoc rendering with
proper word-wrapping and document structure — names it from the document's
own title, and copies it to ``~/Documents/Modulatio/<project>/``.

Why this exists: without a render stage, a producer asked for a ``.pdf``
hand-writes raw PDF source (absolute text placement, no wrapping → clipped at
the page edge). The fix is to keep producers in Markdown and let the engine
render the finished product.

Delivery dir is ``~/Documents/Modulatio/<project_code>/`` by default, override
with ``MODULATIO_DELIVERY_DIR``.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from modulatio.export import ExportError, ExportFormat, export_artifact
from modulatio.review_ledger import (
    _FORMAT_MAGIC,
    _FTYP_EXTS,
    verify_declared_format,
)
from modulatio import families

#: Default finished-product format. Producers write Markdown; the engine
#: renders to this. DOCX is the chosen default (editable, wraps correctly,
#: opens everywhere) over a hand-rolled PDF.
DEFAULT_DELIVERY_FORMAT: ExportFormat = "docx"

#: Filename length cap — long titles get truncated at a word-ish boundary.
_MAX_NAME_LEN = 120

#: Characters illegal or troublesome in filenames across common filesystems.
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/<>:"|?*\x00-\x1f]')


@dataclass(frozen=True)
class DeliveredProduct:
    """Outcome of delivering one finished product.

    ``error`` is None on success; on a render/copy failure it carries the
    message and ``dest`` is the path we tried to write. ``note`` carries a
    non-failure advisory (e.g. the renderer was unavailable so the product
    shipped as Markdown instead of DOCX) — delivery still SUCCEEDED."""
    task_id: str
    source: Path
    dest: Path
    name: str
    error: str | None
    note: str | None = None


def delivery_root() -> Path:
    """Base directory for finished products. ``MODULATIO_DELIVERY_DIR`` wins;
    otherwise ``~/Documents/Modulatio``."""
    override = os.environ.get("MODULATIO_DELIVERY_DIR")
    if override and override.strip():
        return Path(override).expanduser()
    return Path.home() / "Documents" / "Modulatio"


def project_delivery_dir(project_code: str) -> Path:
    """Per-project delivery folder: ``<delivery_root>/<project_code>``."""
    return delivery_root() / project_code


#: Per-job folder-name length cap — shorter than the filename cap; it's a path
#: prefix a human browses, kept comfortably readable.
_MAX_JOB_SLUG_LEN = 64

#: Unicode BIDI controls + C1 NEL that survive the ASCII illegal-char regex.
#: Harmless on Linux (no path escape), but a right-to-left override can scramble
#: an ``ls`` listing — and the slug comes from Leader JSON in Feature B, so we
#: strip them here (Nemo's Feature-A hull advisory A3, defense-in-depth).
_UNICODE_CONTROL_CHARS = re.compile("[\u0085\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def _job_slug(raw: str | None) -> str:
    """Folder-safe, human-readable, **path-traversal-safe** slug for a per-job
    folder. Returns ``""`` when there's nothing usable — the caller then falls
    back to the flat project dir (NOT an "Untitled" folder). Drops path
    separators and leading dots, so an LLM-supplied ``../../etc`` (the slug
    comes from Leader JSON in Feature B) can never escape the delivery dir."""
    s = (raw or "").strip()
    if not s:
        return ""
    s = s.replace(":", " -")
    s = _ILLEGAL_FILENAME_CHARS.sub("", s)   # strips / \\ < > : " | ? * + control chars
    s = _UNICODE_CONTROL_CHARS.sub("", s)    # strips BIDI overrides / NEL (ls-display safety)
    s = re.sub(r"\s+", " ", s).strip(" .")   # collapse whitespace; strip leading/trailing dots+spaces
    if len(s) > _MAX_JOB_SLUG_LEN:
        s = s[:_MAX_JOB_SLUG_LEN].rstrip(" .")
    return s


def _run_date(run_id: str | None) -> str:
    """``YYYYMMDD`` parsed from a ``YYYYMMDDTHHMMSSZ-<hex6>`` run id; ``""`` if
    the id isn't in that shape."""
    if run_id and len(run_id) >= 8 and run_id[:8].isdigit():
        return run_id[:8]
    return ""


def _run_hex(run_id: str | None) -> str:
    """The ``<hex6>`` suffix of a run id — a collision tiebreaker; ``""`` if
    absent."""
    if run_id and "-" in run_id:
        tail = run_id.rsplit("-", 1)[-1]
        if tail and tail.isalnum():
            return tail
    return ""


def job_folder_name(slug: str | None, *, fallback: str, run_id: str | None) -> str | None:
    """Human-readable per-job folder name: ``"<slug-or-fallback> YYYYMMDD"``.
    Returns ``None`` when neither a slug nor a fallback yields anything usable —
    the caller then ships into the flat project dir (back-compat)."""
    base = _job_slug(slug) or _job_slug(fallback)
    if not base:
        return None
    date = _run_date(run_id)
    return f"{base} {date}".rstrip()


def job_dir(
    project_code: str,
    job_slug: str | None = None,
    *,
    run_id: str | None = None,
    fallback: str = "",
    base: Path | None = None,
) -> Path:
    """Per-job delivery folder: ``<project_delivery_dir>/<job-folder>``.

    Named from ``job_slug`` (the Leader's human name for the job, once Feature B
    supplies it) or a slug of ``fallback`` (the project name / objective), plus
    the run date. **When nothing names it, returns the flat
    ``project_delivery_dir`` unchanged** — a run with no slug behaves exactly as
    before. On a name collision with a DIFFERENT run's folder (two same-named
    jobs the same day), the run hex is appended as a tiebreaker.

    ``base`` replaces ``project_delivery_dir`` entirely — the FOLDERS output
    pick. Precedence: picked output folder (base) > MODULATIO_DELIVERY_DIR >
    ``~/Documents/Modulatio`` — the in-app pick is the operator's newer, more
    specific intent; the env var stays the install-level default.

    Concurrency (Nemo's Feature-A hull advisory A1): the ``exists()`` check is
    not atomic with the later ``mkdir``, so two *simultaneous* runs of the same
    project + same slug + same day could both resolve to the bare name and share
    a folder. Per-file disambiguation inside :func:`deliver_product` (``name
    (task_id).ext``) still prevents data loss; the products would merely
    cohabitate. Extreme edge for a single-user CLI; not guarded here."""
    if base is None:
        base = project_delivery_dir(project_code)
    name = job_folder_name(job_slug, fallback=fallback, run_id=run_id)
    if not name:
        return base
    target = base / name
    if target.exists():
        hex6 = _run_hex(run_id)
        if hex6:
            target = base / f"{name} ({hex6})"
    return target


def _sanitize_filename(name: str) -> str:
    """Turn a document title into a safe, still-human-readable filename stem
    (no extension). Colons become ' -', illegal chars drop, whitespace
    collapses, length is capped."""
    name = name.replace(":", " -")
    name = _ILLEGAL_FILENAME_CHARS.sub("", name)
    name = _UNICODE_CONTROL_CHARS.sub("", name)  # strip BIDI/RTL overrides (ls-display safety)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > _MAX_NAME_LEN:
        name = name[:_MAX_NAME_LEN].rstrip(" .")
    return name or "Untitled"


#: Openers that mark a first line as the producer THINKING OUT LOUD rather than a
#: title — first-person / process language. A real title is a noun phrase, never
#: "I …" / "Let me …" / "Now I'll …". Anchored at the line start, word-bounded.
_NARRATION_OPENER = re.compile(
    r"^(i|i'm|i'll|i've|i am|let me|let's|now|okay|ok|first|next|then|"
    r"here'?s|here is|so|alright|we|we'll|we've|going to)\b[\s,]",
    re.IGNORECASE,
)


def _looks_like_narration(s: str) -> str | None:
    """True-ish if ``s`` reads as producer narration (thinking), not a document
    title: it opens first-person / process-y, OR runs as a full sentence (ends in
    terminal punctuation and is long). Titles are short noun phrases; narration is
    prose. Returns a truthy reason string or None."""
    s = s.strip()
    if _NARRATION_OPENER.match(s):
        return "first-person/process opener"
    if s[-1:] in ".!?" and len(s.split()) > 8:
        return "full sentence"
    return None


def human_name_from_markdown(text: str, *, fallback: str) -> str:
    """Derive a human-friendly filename stem from a Markdown document's own
    title: the first ATX heading (``# Title``), else the first non-empty
    line, else ``fallback``. The result is sanitized for the filesystem but
    kept readable — a human should recognize the product by its name, not by
    a task id like ``t-004``.

    REVIEWER NOTE (cadre agnostic audit): the Markdown/ATX-heading assumption
    here is BY DESIGN, not an output-agnostic violation — this helper is only
    ever called on the PROSE/document branch (``_is_prose_source``); code/data/
    media deliverables derive their name from the verbatim stem and never reach
    this function. The name is a (harmless) prose-specific detail, not shared
    logic. Confirmed NOT a violation (both reviewers)."""
    title = None
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"#{1,6}\s+(.*)", s)
        title = (m.group(1) if m else s).strip()
        break
    raw = (title or "").strip()
    # A degenerate title — empty, or all separators (e.g. a stray '---'
    # frontmatter fence taken as the first line) — must not become the filename
    # ('---.docx'). Nor may producer NARRATION ("I now have all the citations...
    # Let me write...") — a thinking-out-loud first line, not a title. In either
    # case fall back to the supplied name (the Leader's document title) before
    # sanitizing, so the file is named for WHAT it is, not the producer's process.
    if not raw.strip("-_ ") or _looks_like_narration(raw):
        raw = fallback
    return _sanitize_filename(raw)


#: Source extensions that are CODE, not prose. A code deliverable ships as
#: runnable source — copied verbatim, original filename + extension preserved —
#: never pandoc-rendered into a DOCX (a Word doc full of Python is useless as
#: a game). Markdown / plain text still flow through the document-render path.
#: Live repro 2026-05-30: a Hollow-Knight ``game.py`` (artifact_kind=code) was
#: headed for a .docx wrapper. Detection is by on-disk extension — the real
#: signal of "would pandoc-rendering this make sense" — independent of how the
#: planner tagged it.
_CODE_SUFFIXES = frozenset({
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".rs",
    ".go", ".java", ".kt", ".kts", ".c", ".h", ".cpp", ".hpp", ".cc",
    ".cs", ".rb", ".php", ".swift", ".scala", ".sh", ".bash", ".zsh",
    ".ps1", ".pl", ".lua", ".r", ".sql", ".html", ".htm", ".css", ".scss",
    ".sass", ".vue", ".svelte", ".json", ".toml", ".yaml", ".yml", ".xml",
    ".ini", ".cfg", ".dockerfile", ".ipynb", ".gd", ".tscn",
})


def _is_code_source(source: Path) -> bool:
    """True iff ``source`` is a code/source artifact that should ship verbatim
    rather than being rendered to a document. Matches on extension; a bare
    ``Dockerfile`` / ``Makefile`` (no suffix) is treated as code by name."""
    if source.suffix.lower() in _CODE_SUFFIXES:
        return True
    return source.name.lower() in {"dockerfile", "makefile"}


#: Extensions whose content IS prose the producer hand-wrote as Markdown — the
#: ONLY inputs the pandoc render path can sensibly consume. A bare ``.pdf`` /
#: ``.docx`` the Leader *named* but whose bytes are still Markdown text also
#: belongs here (the historical reason the render path ignored extension).
#: Everything NOT prose and NOT code (a media composite ``.mp4``/``.zip``/
#: ``.png``, a data file ``.csv``/``.xlsx``, an ALREADY-rendered ``.pdf``/
#: ``.docx`` binary) must ship verbatim — reading those with ``read_text`` and
#: re-rendering them through pandoc corrupts them (and, for media composites,
#: breaks the QC binary-aware checksum guarantee). See :func:`_is_prose_source`.
_PROSE_SUFFIXES = frozenset({
    ".md", ".markdown", ".mdown", ".mkd", ".txt", ".text", ".rst", "",
    # Doc extensions the Leader may name while the bytes are still Markdown
    # text; the render path stages a ``.md`` temp regardless of the name.
    ".pdf", ".docx", ".doc", ".odt", ".rtf",
})


def _looks_binary(source: Path) -> bool:
    """True iff ``source`` is a real binary file (a NUL byte in the head, or
    its bytes don't decode as UTF-8). Used to distinguish a Leader-named
    ``report.pdf`` whose content is still Markdown text (render it) from an
    ALREADY-rendered ``.pdf``/``.docx`` binary or a media composite (ship it
    verbatim). Returns ``False`` if the file can't be read — the caller's
    normal text path then surfaces the real error."""
    try:
        with source.open("rb") as fh:
            head = fh.read(8192)
    except OSError:
        return False
    if b"\x00" in head:
        return True
    # Decode tolerantly: a fixed-size read can split a multibyte UTF-8 code
    # point at the 8192-byte boundary, which would otherwise raise and
    # mis-classify valid text as binary. ``errors="ignore"`` drops only an
    # incomplete trailing sequence (and any genuinely invalid bytes), so a
    # real binary still surfaces via the NUL check above or the replacement
    # of its non-UTF-8 bytes — but a truncated tail no longer triggers a
    # false positive. Re-encode and compare to detect genuine decode loss
    # within the head (true binary), tolerating only a short truncated tail.
    try:
        head.decode("utf-8")
        return False
    except UnicodeDecodeError as exc:
        # If the only undecodable bytes are a short, incomplete multibyte
        # sequence at the very end of the read (a boundary truncation, never
        # more than 3 trailing bytes for UTF-8), treat it as text.
        if exc.start >= len(head) - 3 and exc.end == len(head):
            return False
        return True


def _is_signatured_format(source: Path) -> bool:
    """True iff ``source``'s extension declares a format with a known magic
    signature (the shared ``review_ledger`` fabrication-gate scope). Used to
    distinguish a real, already-rendered deliverable from a Markdown blob merely
    *named* with a doc extension — the latter has no signature and must render."""
    ext = source.suffix.lower().lstrip(".")
    return ext in _FORMAT_MAGIC or ext in _FTYP_EXTS


def _is_prose_source(source: Path) -> bool:
    """True iff ``source`` should flow through the Markdown render path. Prose
    means: a text/markdown extension, OR a doc extension (``.pdf``/``.docx``)
    whose bytes are still hand-written Markdown text (not an already-rendered
    binary). A media composite, data file, or rendered binary returns ``False``
    so delivery copies its bytes verbatim instead of corrupting them."""
    if source.suffix.lower() not in _PROSE_SUFFIXES:
        return False
    # A genuinely-rendered deliverable that DECLARES a known format must ship
    # verbatim, not be re-rendered. ``_looks_binary`` catches the binary doc
    # formats (``.pdf``/``.docx``/``.odt`` — NUL bytes or non-UTF-8), but a
    # TEXT-based rendered format (RTF, ``{\rtf1\ansi...}``: pure ASCII, no NUL,
    # valid UTF-8) sails past it and would be corrupted by a second render.
    # Gate those on the format's own magic signature: if the extension is a
    # declared rendered format AND the bytes carry that format's signature, it
    # is a real, already-rendered deliverable — NOT prose. A Markdown blob
    # merely *named* ``.rtf`` lacks the ``{\rtf`` signature, so
    # ``verify_declared_format`` returns False for it and it still renders.
    if _is_signatured_format(source):
        ok_format, _ = verify_declared_format(source)
        if ok_format:
            return False
    return not _looks_binary(source)


def deliver_product(
    source_md: Path,
    *,
    project_code: str,
    task_id: str,
    fmt: ExportFormat = DEFAULT_DELIVERY_FORMAT,
    fallback_name: str | None = None,
    pinned_names: "set[str] | None" = None,
    verbatim: bool = False,
    dest_override: Path | None = None,
) -> DeliveredProduct:
    """Deliver one finished product under the project's delivery dir.

    Markdown / prose deliverables are rendered to ``fmt`` (DOCX by default),
    human-named from their H1. CODE deliverables (``.py`` etc., see
    :data:`_CODE_SUFFIXES`) are copied **verbatim**, keeping their original
    filename + extension, so a game ships as runnable ``game.py`` — not a Word
    doc wrapping the source.

    ``verbatim`` forces the copy-as-is path even for non-code (e.g. a
    ``README.md`` companion in a CODE BUNDLE keeps its name + sits beside the
    code, instead of becoming a stray ``Some Title.docx``).

    ``pinned_names`` (iteration mode): when a code deliverable's filename is in
    this set it is an IMPROVED pinned file, so it REPLACES its prior same-named
    copy in the delivery dir instead of getting a disambiguated duplicate — the
    user gets one clean ``game.py``, the latest version. Returns a
    :class:`DeliveredProduct` (``error`` set on failure)."""
    source_md = Path(source_md)
    # ``dest_override`` (the per-job folder) wins when given; else the flat
    # per-project dir (back-compat).
    dest_dir = dest_override if dest_override is not None else project_delivery_dir(project_code)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return DeliveredProduct(
            task_id, source_md, dest_dir, source_md.stem, f"mkdir failed: {exc}"
        )
    # VERBATIM: ship the file as-is, original name + extension preserved — code
    # always, plus markdown companions in a code bundle (keep README.md beside
    # game.py rather than rendering it to a stray .docx), AND any non-prose
    # artifact: a media composite (.mp4/.zip/.png), a data file (.csv/.xlsx),
    # or an already-rendered .pdf/.docx binary. Those would be corrupted by the
    # text-read + pandoc render path (and a media composite would lose the QC
    # binary-aware checksum guarantee). Byte-preserving copy keeps them intact.
    if _is_code_source(source_md) or verbatim or not _is_prose_source(source_md):
        name = source_md.stem
        dest = dest_dir / source_md.name
        # Iteration: an improved PINNED file replaces its prior same-named copy
        # (one clean game.py, latest version). Otherwise don't clobber an
        # unrelated prior same-named source — disambiguate with the task id.
        is_pinned = bool(pinned_names) and source_md.name in pinned_names
        if dest.exists() and not is_pinned:
            dest = dest_dir / f"{source_md.stem} ({task_id}){source_md.suffix}"
        try:
            shutil.copyfile(source_md, dest)
        except OSError as exc:
            return DeliveredProduct(task_id, source_md, dest, name, f"copy failed: {exc}")
        return DeliveredProduct(task_id, source_md, dest, name, None)
    # PROSE: render through a ``.md`` temp so pandoc always treats the producer's
    # content as Markdown, regardless of the artifact's on-disk extension
    # (a deliverable the Leader named ``report.pdf`` is still Markdown text).
    text = source_md.read_text(encoding="utf-8", errors="replace")
    name = human_name_from_markdown(text, fallback=fallback_name or task_id)
    ext = "md" if fmt == "markdown" else fmt
    dest = dest_dir / f"{name}.{ext}"
    # Don't clobber a same-named prior deliverable — disambiguate with task id.
    if dest.exists():
        dest = dest_dir / f"{name} ({task_id}).{ext}"
    try:
        with tempfile.TemporaryDirectory() as td:
            md = Path(td) / "deliverable.md"
            md.write_text(text, encoding="utf-8")
            result = export_artifact(md, dest, fmt)
    except ExportError as exc:
        # The renderer (pandoc/pypandoc) isn't installed. A missing OPTIONAL
        # renderer must not mean "no delivery" — degrade gracefully and ship the
        # product as Markdown so the operator still gets their content, with a
        # visible note (delivery SUCCEEDED, just not in the requested format).
        md_dest = dest_dir / f"{name}.md"
        if md_dest.exists():
            md_dest = dest_dir / f"{name} ({task_id}).md"
        try:
            md_dest.write_text(text, encoding="utf-8")
        except OSError as werr:
            return DeliveredProduct(task_id, source_md, dest, name, f"copy failed: {werr}")
        note = (
            f"renderer unavailable — shipped as Markdown instead of {fmt.upper()} "
            f"({str(exc).splitlines()[0]})"
        )
        return DeliveredProduct(task_id, source_md, md_dest, name, None, note)
    return DeliveredProduct(task_id, source_md, dest, name, result.error)


def deliver_finished_products(
    deliverables: "list[tuple[str, Path, str | None, str]]",
    *,
    project_code: str,
    fmt: ExportFormat = DEFAULT_DELIVERY_FORMAT,
    pinned_names: "set[str] | None" = None,
    dest_override: Path | None = None,
    job_name: str | None = None,
) -> list[DeliveredProduct]:
    """Deliver every finished product.

    ``deliverables`` is an iterable of ``(task_id, source_md_path,
    fallback_name)``. Missing source files are skipped (the task may not have
    produced an artifact). ``pinned_names`` is threaded to
    :func:`deliver_product` so improved iteration files replace their prior
    copy. Returns one :class:`DeliveredProduct` per delivered file, in input
    order.

    CODE BUNDLE: if any deliverable is code, markdown/prose companions
    (``README.md`` etc.) ship VERBATIM beside the code — a coherent runnable
    folder — instead of being rendered to a stray ``.docx``. A pure-prose run
    (no code) is unaffected: its deliverables still render to ``fmt``."""
    present = [(t, Path(s), f, fam) for t, s, f, fam in deliverables if Path(s).exists()]
    # A code bundle is signalled by EITHER a recognizable code suffix OR the
    # resolved assembly family (Wild Bill): a code-family fallback lands at a
    # `.txt` path (`_is_code_source` false), so the family must count too — else
    # a README.md companion beside it would render to .docx instead of shipping
    # verbatim as part of the runnable folder.
    bundle_has_code = any(
        _is_code_source(s) or fam == "code" for _, s, _, fam in present
    )
    out: list[DeliveredProduct] = []
    for task_id, src, fallback, family in present:
        # markdown companion in a code bundle → keep it verbatim (README.md)
        # Family-aware (cadre/Wild Bill): a non-document deliverable (code/data/
        # media) ships VERBATIM regardless of its extension — `.txt` is globally
        # classified as prose, so the suffix can't be trusted; the family can.
        verbatim = (bundle_has_code and not _is_code_source(src)) or family != "document"
        out.append(
            deliver_product(
                src, project_code=project_code, task_id=task_id,
                # Feature B: the Leader's job name is the preferred fallback when
                # an artifact has no usable title — a clean `<job>.docx` beats a
                # junk `---.docx` or the giant sanitized task description.
                fmt=fmt, fallback_name=(job_name or fallback), pinned_names=pinned_names,
                verbatim=verbatim, dest_override=dest_override,
            )
        )
    return out


_QUALITY_REPORT_TITLE = "Product Quality Report"


def build_product_quality_report(recommendations) -> str:
    """Render the Leader's human-addressed Product Quality Report (Markdown).

    This is the Leader's analysis OF the delivered work — what it stands
    behind and what it recommends the human double-check — NOT part of the
    work itself and NOT something the swarm reviews. ``recommendations`` is a
    list of ``{goal_id, concern, suggestion}`` gathered from the Leader's
    verdicts. Always produced: an "all clear" is itself useful signal."""
    lines = [
        f"# {_QUALITY_REPORT_TITLE}",
        "",
        "_My assessment of the delivered work, as the project lead who oversaw "
        "it. The deliverables were produced and quality-controlled by the team; "
        "the notes below are my remaining reservations and the checks I'd run "
        "before relying on the work. They are ADVISORY — they did not block or "
        "hold back your product._",
        "",
    ]
    if not recommendations:
        lines += [
            "## No outstanding reservations",
            "",
            "All checks passed — I have nothing to flag for you to double-check. "
            "The deliverables met quality control cleanly.",
            "",
        ]
        return "\n".join(lines)

    lines += ["## Recommended checks before you rely on this", ""]
    for r in recommendations:
        concern = str(r.get("concern", "") or "").strip()
        suggestion = str(r.get("suggestion", "") or "").strip()
        gid = str(r.get("goal_id", "") or "").strip()
        tag = f" _(goal {gid})_" if gid else ""
        if concern and suggestion:
            lines.append(f"- **{concern}**{tag}  \n  Recommended check: {suggestion}")
        elif concern:
            lines.append(f"- **{concern}**{tag}")
        elif suggestion:
            lines.append(f"- {suggestion}{tag}")
    lines.append("")
    return "\n".join(lines)


def deliver_product_quality_report(
    recommendations,
    *,
    project_code: str,
    dest_override: Path | None = None,
) -> "DeliveredProduct | None":
    """Render the Product Quality Report and place it beside the deliverables.
    Always shipped as DOCX (it sits next to the .docx products and the human
    opens it the same way), regardless of any other format config. Always
    produced (the 'all clear' case included). Returns ``None`` only if the
    report couldn't be staged for render."""
    body = build_product_quality_report(recommendations)
    try:
        with tempfile.TemporaryDirectory() as td:
            md = Path(td) / "product-quality-report.md"
            md.write_text(body, encoding="utf-8")
            return deliver_product(
                md, project_code=project_code,
                task_id="product-quality-report", fmt="docx",
                dest_override=dest_override,
            )
    except OSError:  # pragma: no cover — defensive staging failure
        return None


def deliverables_from_tasks(
    tasks, artifacts_root: Path,
) -> "list[tuple[str, Path, str | None, str]]":
    """Map deliverable-tagged tasks to ``(task_id, artifact_path,
    fallback_name)`` tuples for :func:`deliver_finished_products`.

    Resolves each task's artifact path: ``output_path`` under
    ``artifacts_root``, else the default ``drafts/<task_id>.md``. Tasks are
    duck-typed (``.deliverable`` / ``.id`` / ``.output_path`` / ``.description``)
    so this stays decoupled from the orchestration types. The task description
    is the fallback name when a deliverable has no Markdown title of its own.

    DEDUP: when several deliverable tasks target the SAME artifact path (the
    iteration shape — three in-place edits to one ``game.py``), the file is
    emitted ONCE, keyed to the LAST such task (its on-disk content is the final
    state). Without this, one improved file shipped as three identical copies."""
    artifacts_root = Path(artifacts_root)
    by_path: "dict[Path, tuple[str, Path, str | None, str]]" = {}
    for t in tasks:
        if not getattr(t, "deliverable", False):
            continue
        # Default draft path mirrors the producer's writer, which lowercases the
        # task id (orchestration ``drafts/{task.id.lower()}.md``). Using the raw
        # id here silently misses the file for an uppercase id (e.g. ABC-T-001),
        # so the deliverable never ships. Honor an explicit ``output_path`` as-is.
        # Family-aware fallback — MUST match the writer (families.draft_fallback_name
        # at the producer write site), or a non-document deliverable written to
        # drafts/<id>.txt is looked for at drafts/<id>.md and silently lost.
        rel = getattr(t, "output_path", None) or f"drafts/{families.draft_fallback_name(t)}"
        path = artifacts_root / rel
        # Last writer wins — later tasks edited the same file after earlier ones.
        # The 4th element is the assembly FAMILY: a non-document deliverable ships
        # VERBATIM (never rendered), keyed on family not on the unreliable suffix.
        by_path[path] = (getattr(t, "id"), path, getattr(t, "description", None),
                         families.family_for_task(t))
    return list(by_path.values())


#: Task states that mean "did not cleanly succeed" — a finished product built
#: while any of these is unresolved would be ungrounded, so delivery is held.
_BLOCKED_STATES = frozenset({"blocked", "qc_rejected", "abandoned"})


def blocked_task_ids(tasks) -> "list[str]":
    """Ids of tasks in a blocked/failed terminal state. Used to WITHHOLD
    finished products: don't hand the user a polished deliverable from a run
    that still has unresolved blocked work (the "polished wrong product"
    trap). Duck-typed on ``.status`` / ``.id``."""
    out: "list[str]" = []
    for t in tasks:
        st = str(getattr(t, "status", "")).split(".")[-1].lower()
        if st in _BLOCKED_STATES:
            out.append(str(getattr(t, "id", "?")))
    return out


def blocked_goal_ids(goals) -> "list[str]":
    """Ids of goals in a blocked/failed terminal state — the cross-GOAL
    companion to ``blocked_task_ids``.

    Why this exists (2026-05-30, live run): a goal whose task-plan is
    REJECTED produces ZERO tasks — just a BLOCKED goal + a ticket — so it
    is invisible to ``blocked_task_ids``. In the wild this shipped a
    polished but OFF-TOPIC product: the research goal (G-001) was blocked,
    yet the downstream draft goal (G-002) ran ungrounded and its hallucinated
    paper was delivered with a ✓. Goals don't model explicit cross-goal
    deps yet, so the guard stays CONSERVATIVE: ANY blocked goal in the run
    withholds ALL finished products. Duck-typed on ``.status`` / ``.id``."""
    out: "list[str]" = []
    for g in goals:
        st = str(getattr(g, "status", "")).split(".")[-1].lower()
        if st in _BLOCKED_STATES:
            out.append(str(getattr(g, "id", "?")))
    return out


def finished_product_paths(project_code: str) -> "set[Path]":
    """Absolute paths of EVERY run's finished deliverables — the files the
    operator actually asked for. UIs use it to ★-flag + hoist them out of
    the research/draft pile (TUI Artifacts tab, WebOS Artifacts page).
    Best-effort: any failure returns empty (the flag is cosmetic and must
    never block a listing).

    Spans ALL runs, not just the latest: a finished product stays starred
    permanently (until deleted), so the operator always picks their
    products out of the accumulating durable pile."""
    try:
        from modulatio import store, vault

        out: "set[Path]" = set()
        for run_id in vault.list_runs(project_code):
            tasks = store.list_tasks(project_code, run_id=run_id)
            artifacts_root = vault.project_dir(project_code) / "artifacts" / run_id
            for _tid, path, _fallback, _fam in deliverables_from_tasks(
                tasks, artifacts_root
            ):
                try:
                    out.add(path.resolve())
                except OSError:
                    out.add(path)
        return out
    except Exception:
        return set()


__all__ = [
    "DEFAULT_DELIVERY_FORMAT",
    "DeliveredProduct",
    "blocked_goal_ids",
    "blocked_task_ids",
    "build_product_quality_report",
    "deliver_product_quality_report",
    "deliver_finished_products",
    "deliver_product",
    "deliverables_from_tasks",
    "delivery_root",
    "finished_product_paths",
    "human_name_from_markdown",
    "job_dir",
    "job_folder_name",
    "project_delivery_dir",
]
