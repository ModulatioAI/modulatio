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
import tempfile
from dataclasses import dataclass
from pathlib import Path

from modulatio.export import ExportError, ExportFormat, export_artifact

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
    message and ``dest`` is the path we tried to write."""
    task_id: str
    source: Path
    dest: Path
    name: str
    error: str | None


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


def _sanitize_filename(name: str) -> str:
    """Turn a document title into a safe, still-human-readable filename stem
    (no extension). Colons become ' -', illegal chars drop, whitespace
    collapses, length is capped."""
    name = name.replace(":", " -")
    name = _ILLEGAL_FILENAME_CHARS.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > _MAX_NAME_LEN:
        name = name[:_MAX_NAME_LEN].rstrip(" .")
    return name or "Untitled"


def human_name_from_markdown(text: str, *, fallback: str) -> str:
    """Derive a human-friendly filename stem from a Markdown document's own
    title: the first ATX heading (``# Title``), else the first non-empty
    line, else ``fallback``. The result is sanitized for the filesystem but
    kept readable — a human should recognize the product by its name, not by
    a task id like ``t-004``."""
    title = None
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"#{1,6}\s+(.*)", s)
        title = (m.group(1) if m else s).strip()
        break
    return _sanitize_filename((title or "").strip() or fallback)


def deliver_product(
    source_md: Path,
    *,
    project_code: str,
    task_id: str,
    fmt: ExportFormat = DEFAULT_DELIVERY_FORMAT,
    fallback_name: str | None = None,
) -> DeliveredProduct:
    """Render one Markdown artifact to ``fmt`` and place it, human-named,
    under the project's delivery dir. Returns a :class:`DeliveredProduct`
    (``error`` set on failure)."""
    source_md = Path(source_md)
    text = source_md.read_text(errors="replace")
    name = human_name_from_markdown(text, fallback=fallback_name or task_id)
    dest_dir = project_delivery_dir(project_code)
    ext = "md" if fmt == "markdown" else fmt
    dest = dest_dir / f"{name}.{ext}"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return DeliveredProduct(task_id, source_md, dest, name, f"mkdir failed: {exc}")
    # Don't clobber a same-named prior deliverable — disambiguate with task id.
    if dest.exists():
        dest = dest_dir / f"{name} ({task_id}).{ext}"
    # Render through a ``.md`` temp so pandoc always treats the producer's
    # content as Markdown, regardless of the artifact's on-disk extension
    # (a deliverable the Leader named ``report.pdf`` is still Markdown text).
    try:
        with tempfile.TemporaryDirectory() as td:
            md = Path(td) / "deliverable.md"
            md.write_text(text)
            result = export_artifact(md, dest, fmt)
    except ExportError as exc:
        return DeliveredProduct(task_id, source_md, dest, name, str(exc))
    return DeliveredProduct(task_id, source_md, dest, name, result.error)


def deliver_finished_products(
    deliverables: "list[tuple[str, Path, str | None]]",
    *,
    project_code: str,
    fmt: ExportFormat = DEFAULT_DELIVERY_FORMAT,
) -> list[DeliveredProduct]:
    """Deliver every finished product.

    ``deliverables`` is an iterable of ``(task_id, source_md_path,
    fallback_name)``. Missing source files are skipped (the task may not have
    produced an artifact). Returns one :class:`DeliveredProduct` per delivered
    file, in input order."""
    out: list[DeliveredProduct] = []
    for task_id, src, fallback in deliverables:
        src = Path(src)
        if not src.exists():
            continue
        out.append(
            deliver_product(
                src, project_code=project_code, task_id=task_id,
                fmt=fmt, fallback_name=fallback,
            )
        )
    return out


def deliverables_from_tasks(
    tasks, artifacts_root: Path,
) -> "list[tuple[str, Path, str | None]]":
    """Map deliverable-tagged tasks to ``(task_id, artifact_path,
    fallback_name)`` tuples for :func:`deliver_finished_products`.

    Resolves each task's artifact path: ``output_path`` under
    ``artifacts_root``, else the default ``drafts/<task_id>.md``. Tasks are
    duck-typed (``.deliverable`` / ``.id`` / ``.output_path`` / ``.description``)
    so this stays decoupled from the orchestration types. The task description
    is the fallback name when a deliverable has no Markdown title of its own."""
    artifacts_root = Path(artifacts_root)
    out: "list[tuple[str, Path, str | None]]" = []
    for t in tasks:
        if not getattr(t, "deliverable", False):
            continue
        rel = getattr(t, "output_path", None) or f"drafts/{getattr(t, 'id')}.md"
        out.append((
            getattr(t, "id"),
            artifacts_root / rel,
            getattr(t, "description", None),
        ))
    return out


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


__all__ = [
    "DEFAULT_DELIVERY_FORMAT",
    "DeliveredProduct",
    "blocked_goal_ids",
    "blocked_task_ids",
    "deliver_finished_products",
    "deliver_product",
    "deliverables_from_tasks",
    "delivery_root",
    "human_name_from_markdown",
    "project_delivery_dir",
]
