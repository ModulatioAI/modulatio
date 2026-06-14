# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""QC history — precedent log for recursive learning (slice #8.1).

Every QC verdict is appended to ``<project_vault>/qc-history/<domain>/``
as one markdown file per verdict (entry_id + timestamp in the filename
for chronological ordering). Markdown is the source of truth; humans
can prune bad precedent by deleting a file, no code needed.

A LanceDB cache under ``<config.get_cache_root()>/qc-history/<project>/<domain>/``
derives embedded vectors of each verdict's artifact body so the QC skill
can pull the top-K most-similar prior verdicts on its next call. First
use of the MiniLM + LanceDB stack outside of agent routing — scope
discipline per ``semantic_router``'s lesson applies here: one purpose,
one index, rebuild from the filesystem when a hash changes.

Slice scope (slice #8.1):
- Append on every verdict (pass or fail — both teach).
- Retrieval by cosine similarity against the artifact body.
- Per-(project, domain) LanceDB table. No cross-domain mixing.

cache root + embedding
model identifier are now resolved from ``config`` on every call so
the wizard's overrides (``cache_root`` / ``embedding_model``) take
effect here the same way they do in ``semantic_router`` and team
memory. The prior module-level constants drifted from config and
caused retrieval to use one model while the canonical hash thought
it used another.

Write-side of the Research-First pattern (standards-via-QC proposals) is
slice #10. Human training notes injected into the QC prompt is slice #8.2.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from modulatio import config
from modulatio.semantic_router import Embedder
from modulatio.vault import project_dir

# Per-(project, domain) reentrant lock guarding the LanceDB "verdicts" index.
# QC runs per task on concurrent wave workers (default-on); each calls
# similar_verdicts() -> _ensure_verdict_vectors(), which does a destructive
# drop_table+create_table rebuild, then reads the table. Without serialization
# two QC workers of the SAME artifact_kind (== same qc-history domain) can both
# rebuild at once (create_table races) or one can drop/recreate the table while
# another is mid-search — raise or garbled precedent. This is the exact hazard
# 5bce6a2 fixed in team_memory; the structurally-identical sibling was missed.
# Keyed per-(project, domain) so unrelated artifact kinds don't contend (domain
# == LanceDB table granularity). Reentrant so similar_verdicts() can hold it
# across both _ensure_verdict_vectors() and its own read.
_VECTOR_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_VECTOR_LOCKS_GUARD = threading.Lock()


def _vector_lock(project_code: str, domain: str) -> threading.RLock:
    key = (project_code, domain)
    with _VECTOR_LOCKS_GUARD:
        lock = _VECTOR_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _VECTOR_LOCKS[key] = lock
        return lock


def _cache_root() -> Path:
    """Resolve the QC-history cache root from current config. Honors
    the wizard's ``cache_root`` override; falls back to the package
    XDG default. Per-call resolution means tests + runtime config
    changes both work without restart."""
    return config.get_cache_root() / "qc-history"


def _embed_model() -> str:
    """Resolve the active embedding-model identifier from current
    config. Used as a fingerprint in the LanceDB metadata so a model
    swap triggers a rebuild rather than serving stale vectors."""
    return config.get_embedding_model()

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_ARTIFACT_BLOCK_RE = re.compile(
    r">>>ARTIFACT-BODY-START<<<\n(.*?)\n>>>ARTIFACT-BODY-END<<<",
    re.DOTALL,
)
_RATIONALE_BLOCK_RE = re.compile(
    r"## Rationale\n\n(.*?)\n\n## Artifact body",
    re.DOTALL,
)
_FILENAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9-]+")


# ── Record ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VerdictRecord:
    """A single logged QC verdict.

    ``defect_type`` is ``None`` on pass, and one of ``"mechanical"`` /
    ``"substantive"`` / ``"environmental"`` on fail (matches
    ``_qc_review`` output).
    """

    entry_id: str
    timestamp: str
    task_id: str
    producer_agent: str
    qc_agent: str
    verdict: str  # "pass" | "fail"
    defect_type: str | None
    rationale: str
    artifact_body: str


# ── File layout ───────────────────────────────────────────────────────────

def _domain_dir(domain: str, project_code: str) -> Path:
    return project_dir(project_code) / "qc-history" / domain


def _filename_slug(s: str) -> str:
    return _FILENAME_UNSAFE_RE.sub("-", s).strip("-") or "entry"


def _verdict_filename(record: VerdictRecord) -> str:
    # Sortable compact timestamp prefix → append-order sort by filename.
    ts = _filename_slug(record.timestamp)
    eid = _filename_slug(record.entry_id)
    return f"{ts}__{eid}.md"


def _render_verdict(record: VerdictRecord) -> str:
    defect_line = record.defect_type if record.defect_type else ""
    return (
        "---\n"
        f"entry_id: {record.entry_id}\n"
        f"timestamp: {record.timestamp}\n"
        f"task_id: {record.task_id}\n"
        f"producer_agent: {record.producer_agent}\n"
        f"qc_agent: {record.qc_agent}\n"
        f"verdict: {record.verdict}\n"
        f"defect_type: {defect_line}\n"
        "---\n\n"
        "## Rationale\n\n"
        f"{record.rationale}\n\n"
        "## Artifact body\n\n"
        ">>>ARTIFACT-BODY-START<<<\n"
        f"{record.artifact_body}\n"
        ">>>ARTIFACT-BODY-END<<<\n"
    )


def _parse_verdict_file(path: Path) -> VerdictRecord:
    # Decode defensively: the docstring invites humans to hand-prune /
    # drop in history files, so a non-UTF-8 or mangled file is plausible.
    # A bad byte must degrade to a best-effort parse, never raise
    # UnicodeDecodeError (a ValueError) out of load_verdicts and brick the
    # whole domain's precedent (and the QC read path / lessons codification
    # loop that consume it). errors="replace" matches the resilience the
    # sibling text-review path already has.
    raw = path.read_text(encoding="utf-8", errors="replace")
    fm = _FRONTMATTER_RE.match(raw)
    meta: dict[str, str] = {}
    if fm:
        for line in fm.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()

    rationale_match = _RATIONALE_BLOCK_RE.search(raw)
    rationale = rationale_match.group(1).strip() if rationale_match else ""

    artifact_match = _ARTIFACT_BLOCK_RE.search(raw)
    artifact_body = artifact_match.group(1) if artifact_match else ""

    raw_defect = meta.get("defect_type", "").strip()
    defect_type: str | None = raw_defect if raw_defect else None

    return VerdictRecord(
        entry_id=meta.get("entry_id", ""),
        timestamp=meta.get("timestamp", ""),
        task_id=meta.get("task_id", ""),
        producer_agent=meta.get("producer_agent", ""),
        qc_agent=meta.get("qc_agent", ""),
        verdict=meta.get("verdict", ""),
        defect_type=defect_type,
        rationale=rationale,
        artifact_body=artifact_body,
    )


def append_verdict(domain: str, project_code: str, record: VerdictRecord) -> Path:
    """Append a verdict to the domain's history log.

    Creates the domain directory on first use. Returns the file path.
    """
    dir_ = _domain_dir(domain, project_code)
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / _verdict_filename(record)
    path.write_text(_render_verdict(record))
    return path


def load_verdicts(domain: str, project_code: str) -> list[VerdictRecord]:
    """Load every verdict for ``domain`` in append order.

    Missing directory → empty list (fresh project has no precedent yet).
    """
    dir_ = _domain_dir(domain, project_code)
    if not dir_.exists():
        return []
    files = sorted(dir_.glob("*.md"))
    records: list[VerdictRecord] = []
    for p in files:
        try:
            records.append(_parse_verdict_file(p))
        except OSError:
            # One unreadable file (vanished mid-glob, permission error,
            # FS hiccup) must not brick the whole domain's precedent.
            # Decode errors are already absorbed via errors="replace".
            continue
    return records


# ── Embedded retrieval ────────────────────────────────────────────────────

def _cache_dir(project_code: str, domain: str) -> Path:
    return _cache_root() / project_code.lower() / domain


def _meta_path(project_code: str, domain: str) -> Path:
    return _cache_dir(project_code, domain) / "meta.json"


def _db_path(project_code: str, domain: str) -> Path:
    return _cache_dir(project_code, domain) / "lance.db"


def _config_hash(records: list[VerdictRecord]) -> str:
    """Hash keyed on the set of verdicts plus embedding model. Append or
    prune a record → hash changes → rebuild. Embedding model is
    resolved live from config so a wizard model-swap triggers a
    rebuild on next access."""
    ids = "||".join(r.entry_id for r in records)
    return hashlib.sha256(
        (ids + f"|model={_embed_model()}").encode()
    ).hexdigest()[:16]


def _load_meta(project_code: str, domain: str) -> dict:
    p = _meta_path(project_code, domain)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def _save_meta(project_code: str, domain: str, meta: dict) -> None:
    _cache_dir(project_code, domain).mkdir(parents=True, exist_ok=True)
    _meta_path(project_code, domain).write_text(json.dumps(meta, indent=2))


def _ensure_verdict_vectors(
    domain: str, project_code: str, embedder: Embedder,
) -> list[VerdictRecord]:
    """Build/refresh the per-(project, domain) LanceDB index from the
    markdown log. Returns the current record list so callers can short-
    circuit when it's empty.

    Full rebuild on any mismatch (entry set changed, model changed, dim
    changed). Volumes are expected low in slice #8 scope; incremental
    append is a later optimization if the history grows large.
    """
    # Serialized per (project, domain): the drop_table+create_table rebuild is
    # destructive and must not interleave with another worker's rebuild or read.
    with _vector_lock(project_code, domain):
        records = load_verdicts(domain, project_code)
        new_hash = _config_hash(records)
        meta = _load_meta(project_code, domain)

        active_model = _embed_model()
        if (
            meta.get("records_hash") == new_hash
            and meta.get("embedding_model") == active_model
            and meta.get("embed_dim") == embedder.dim
        ):
            return records

        import lancedb
        import pyarrow as pa

        _cache_dir(project_code, domain).mkdir(parents=True, exist_ok=True)
        db = lancedb.connect(_db_path(project_code, domain))

        if "verdicts" in db.list_tables().tables:
            db.drop_table("verdicts")

        if not records:
            _save_meta(project_code, domain, {
                "records_hash": new_hash,
                "embedding_model": active_model,
                "embed_dim": embedder.dim,
                "record_count": 0,
            })
            return records

        texts = [r.artifact_body for r in records]
        vectors = embedder.embed_texts(texts)

        rows = [
            {"vector": v, "entry_id": r.entry_id}
            for r, v in zip(records, vectors)
        ]
        schema = pa.schema([
            pa.field("vector", pa.list_(pa.float32(), embedder.dim)),
            pa.field("entry_id", pa.string()),
        ])
        db.create_table("verdicts", data=rows, schema=schema)

        _save_meta(project_code, domain, {
            "records_hash": new_hash,
            "embedding_model": active_model,
            "embed_dim": embedder.dim,
            "record_count": len(records),
        })
        return records


def similar_verdicts(
    domain: str,
    project_code: str,
    *,
    artifact_body: str,
    embedder: Embedder,
    k: int = 5,
) -> list[tuple[VerdictRecord, float]]:
    """Return up to ``k`` prior verdicts most similar to ``artifact_body``.

    Sorted by cosine similarity descending. Empty history → empty list.
    Domain-scoped: a "code" call never returns "marketing" verdicts.
    """
    # Hold the per-(project, domain) lock across the rebuild AND the read so no
    # concurrent QC worker can drop/recreate the shared table mid-search (the
    # lock is reentrant, so the nested _ensure_verdict_vectors acquire is free).
    with _vector_lock(project_code, domain):
        records = _ensure_verdict_vectors(domain, project_code, embedder)
        if not records:
            return []

        import lancedb

        path = _db_path(project_code, domain)
        if not path.exists():
            return []

        db = lancedb.connect(path)
        if "verdicts" not in db.list_tables().tables:
            return []

        table = db.open_table("verdicts")
        if table.count_rows() == 0:
            return []

        query_vec = embedder.embed_text(artifact_body)
        results = (
            table.search(query_vec)
            .metric("cosine")
            .limit(k)
            .to_list()
        )

        by_id = {r.entry_id: r for r in records}
        hits: list[tuple[VerdictRecord, float]] = []
        for row in results:
            eid = row.get("entry_id")
            rec = by_id.get(eid)
            if rec is None:
                # Stale vector for a record since pruned — skip.
                continue
            distance = float(row.get("_distance", 1.0))
            similarity = 1.0 - distance
            hits.append((rec, similarity))
        return hits


__all__ = [
    "VerdictRecord",
    "append_verdict",
    "load_verdicts",
    "similar_verdicts",
]
