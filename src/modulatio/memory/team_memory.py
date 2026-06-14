# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Team-shared QC memory pool — locked Option B (slice 4).

Built on v2's ``qc_history`` substrate (LanceDB + MiniLM) rather than
porting v1.3.1's 893-line ``crew_memory.py`` verbatim. Same storage
primitive (markdown source of truth + LanceDB vector cache); different
write semantics (ACL + propose-via-QC pattern).

Per locked design (project_modulatio_v2_memory_architecture.md):

- **ACL**: write only for ``tier in {"qc","leader"}``; read for all roster
- **Promotion path**: any agent can ``propose()`` an entry; routes through
  QC review (mirrors slice #12 ``proposed_standard``); on approve, writes
  with attribution
- **Pre-task consultation**: orchestrator calls ``recall(...)`` before
  every dispatch; result injected into producer prompt via new
  ``{team_memory_context}`` slot
- **Targeted recall**: metadata pre-filter (skill+kind+capabilities) →
  top-K semantic similarity → cap K=5. Never returns full pool.

Storage:
    <vault>/<code>/team-memory/*.md       (markdown source of truth)
    ~/.cache/modulatio/team-memory/<code>/ (LanceDB vector cache)
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from modulatio import config
from modulatio.semantic_router import Embedder
from modulatio.vault import project_dir

def _embed_model() -> str:
    """Resolve the active embedding-model identifier from current config.

    Resolved per-call (not captured at import) so a wizard model-swap
    mid-process flows into the LanceDB metadata fingerprint and triggers a
    rebuild rather than serving stale vectors. Mirrors qc_history._embed_model.
    """
    return config.get_embedding_model()

# Per-project reentrant lock guarding the LanceDB index. Concurrent wave
# workers call recall() before every dispatch; _ensure_vectors() does a
# destructive drop_table+create_table rebuild, and recall() then reads the
# table. Without serialization one thread can drop/recreate the shared table
# while another is mid-search (or both rebuild at once), corrupting results or
# raising. Reentrant so recall() can hold it across both _ensure_vectors() and
# its own read. Per-project so unrelated projects don't contend. (A plain
# threading lock — no fork interaction, unlike the run_shell rlimit hazard.)
_VECTOR_LOCKS: dict[str, threading.RLock] = {}
_VECTOR_LOCKS_GUARD = threading.Lock()


def _vector_lock(project_code: str) -> threading.RLock:
    with _VECTOR_LOCKS_GUARD:
        lock = _VECTOR_LOCKS.get(project_code)
        if lock is None:
            lock = threading.RLock()
            _VECTOR_LOCKS[project_code] = lock
        return lock

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_BODY_RE = re.compile(r"## Body\s*\n\n(.*?)\Z", re.DOTALL)
_FILENAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9-]+")

# ACL: only these tiers can write to team memory.
_WRITE_AUTHORIZED_TIERS = frozenset({"qc", "leader"})


class PermissionDenied(Exception):
    """Raised when a non-QC, non-Leader agent attempts a direct write."""


# ── Record ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MemoryEntry:
    """A single team-memory entry. All fields used for filtering + attribution."""

    entry_id: str
    timestamp: str
    writer_id: str
    writer_tier: str  # qc | leader (enforced on write)
    skill_tags: tuple[str, ...]
    artifact_kind: str
    capability_tags: tuple[str, ...]
    body: str
    proposed_by: str | None = None  # agent_id if entry came via propose-then-approve

    def to_frontmatter(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "timestamp": self.timestamp,
            "writer_id": self.writer_id,
            "writer_tier": self.writer_tier,
            "skill_tags": list(self.skill_tags),
            "artifact_kind": self.artifact_kind,
            "capability_tags": list(self.capability_tags),
            "proposed_by": self.proposed_by or "",
        }


# ── File layout ───────────────────────────────────────────────────────────

def _team_dir(project_code: str) -> Path:
    return project_dir(project_code) / "team-memory"


def _filename_slug(s: str) -> str:
    return _FILENAME_UNSAFE_RE.sub("-", s).strip("-") or "entry"


def _entry_filename(record: MemoryEntry) -> str:
    ts = _filename_slug(record.timestamp)
    eid = _filename_slug(record.entry_id)
    return f"{ts}__{eid}.md"


def _render(record: MemoryEntry) -> str:
    fm = record.to_frontmatter()
    lines = ["---"]
    for k, v in fm.items():
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(v)}]")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append("## Body")
    lines.append("")
    lines.append(record.body)
    lines.append("")
    return "\n".join(lines)


def _parse_csv_field(raw: str) -> tuple[str, ...]:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _parse(path: Path) -> MemoryEntry | None:
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        # A binary / non-UTF-8 / truncated-mid-multibyte .md (crash-during-write,
        # FS corruption, a stray file) must degrade to "skip this one entry"
        # rather than bricking list_entries()/recall() for every valid entry.
        # UnicodeDecodeError is a ValueError subclass, not an OSError, so it
        # needs its own arm. Mirrors store._read_entity's quarantine contract.
        return None
    fm = _FRONTMATTER_RE.match(text)
    meta: dict[str, str] = {}
    if fm:
        for line in fm.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()

    body_m = _BODY_RE.search(text)
    body = body_m.group(1).strip() if body_m else ""

    return MemoryEntry(
        entry_id=meta.get("entry_id", ""),
        timestamp=meta.get("timestamp", ""),
        writer_id=meta.get("writer_id", ""),
        writer_tier=meta.get("writer_tier", ""),
        skill_tags=_parse_csv_field(meta.get("skill_tags", "")),
        artifact_kind=meta.get("artifact_kind", ""),
        capability_tags=_parse_csv_field(meta.get("capability_tags", "")),
        body=body,
        proposed_by=meta.get("proposed_by") or None,
    )


# ── Time helpers ──────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Disambiguates ids minted within the same truncated-microsecond window.
# The strftime prefix only has 10us resolution after the [:18] slice, so two
# write()/propose() calls (e.g. concurrent wave workers) landing in the same
# window would otherwise collide and silently overwrite each other's file.
_ID_LOCK = threading.Lock()
_ID_COUNTER = 0


def _new_id() -> str:
    global _ID_COUNTER
    prefix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")[:18]
    with _ID_LOCK:
        _ID_COUNTER = (_ID_COUNTER + 1) % 1000000
        seq = _ID_COUNTER
    return f"{prefix}{seq:06d}"


# ── Public write API (ACL-enforced) ───────────────────────────────────────

def write(
    *,
    writer_id: str,
    writer_tier: str,
    body: str,
    project_code: str,
    skill_tags: tuple[str, ...] | list[str] = (),
    artifact_kind: str = "",
    capability_tags: tuple[str, ...] | list[str] = (),
    proposed_by: str | None = None,
) -> MemoryEntry:
    """Direct write to team memory. ACL-enforced.

    Raises ``PermissionDenied`` if ``writer_tier`` is not 'qc' or 'leader'.
    """
    if writer_tier not in _WRITE_AUTHORIZED_TIERS:
        raise PermissionDenied(
            f"Write to team memory denied for tier '{writer_tier}'. "
            f"Only {sorted(_WRITE_AUTHORIZED_TIERS)} can write directly. "
            f"Use propose() to submit through QC review."
        )

    record = MemoryEntry(
        entry_id=_new_id(),
        timestamp=_now_iso(),
        writer_id=writer_id,
        writer_tier=writer_tier,
        skill_tags=tuple(skill_tags),
        artifact_kind=artifact_kind,
        capability_tags=tuple(capability_tags),
        body=body,
        proposed_by=proposed_by,
    )
    dir_ = _team_dir(project_code)
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / _entry_filename(record)).write_text(_render(record))
    return record


# ── Propose path (any agent → QC review → write on approve) ───────────────

@dataclass
class Proposal:
    """A pending team-memory proposal awaiting QC review.

    Stored separately from approved entries so orchestrator can route to
    QC. On approve, calls ``write`` with the original proposer's id as
    ``proposed_by``.
    """

    proposal_id: str
    proposer_id: str
    timestamp: str
    body: str
    skill_tags: tuple[str, ...]
    artifact_kind: str
    capability_tags: tuple[str, ...]
    rationale: str = ""


def propose(
    *,
    proposer_id: str,
    body: str,
    project_code: str,
    skill_tags: tuple[str, ...] | list[str] = (),
    artifact_kind: str = "",
    capability_tags: tuple[str, ...] | list[str] = (),
    rationale: str = "",
) -> Proposal:
    """Stage an entry for QC review. Does NOT write to team memory yet.

    The orchestrator (slice 4 wiring) routes the proposal to QC; on QC
    approve, calls ``approve_proposal()`` which writes via ``write()``.
    """
    proposal = Proposal(
        proposal_id=_new_id(),
        proposer_id=proposer_id,
        timestamp=_now_iso(),
        body=body,
        skill_tags=tuple(skill_tags),
        artifact_kind=artifact_kind,
        capability_tags=tuple(capability_tags),
        rationale=rationale,
    )
    dir_ = _team_dir(project_code) / "_proposals"
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / f"{_filename_slug(proposal.timestamp)}__{proposal.proposal_id}.json"
    path.write_text(json.dumps({
        "proposal_id": proposal.proposal_id,
        "proposer_id": proposer_id,
        "timestamp": proposal.timestamp,
        "body": body,
        "skill_tags": list(proposal.skill_tags),
        "artifact_kind": artifact_kind,
        "capability_tags": list(proposal.capability_tags),
        "rationale": rationale,
    }, indent=2))
    return proposal


def list_proposals(project_code: str) -> list[Proposal]:
    """Return all pending proposals."""
    dir_ = _team_dir(project_code) / "_proposals"
    if not dir_.exists():
        return []
    out: list[Proposal] = []
    for p in sorted(dir_.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append(Proposal(
            proposal_id=data.get("proposal_id", ""),
            proposer_id=data.get("proposer_id", ""),
            timestamp=data.get("timestamp", ""),
            body=data.get("body", ""),
            skill_tags=tuple(data.get("skill_tags", [])),
            artifact_kind=data.get("artifact_kind", ""),
            capability_tags=tuple(data.get("capability_tags", [])),
            rationale=data.get("rationale", ""),
        ))
    return out


def approve_proposal(
    proposal_id: str,
    *,
    project_code: str,
    approver_id: str,
    approver_tier: str,
) -> MemoryEntry:
    """QC or Leader approves a proposal → writes to team memory + cleans up.

    Raises ``PermissionDenied`` if ``approver_tier`` not in QC/Leader.
    Raises ``KeyError`` if proposal not found.
    """
    if approver_tier not in _WRITE_AUTHORIZED_TIERS:
        raise PermissionDenied(
            f"Approve denied for tier '{approver_tier}'. Only {sorted(_WRITE_AUTHORIZED_TIERS)} can approve."
        )
    dir_ = _team_dir(project_code) / "_proposals"
    matching = [p for p in dir_.glob(f"*{proposal_id}*.json")] if dir_.exists() else []
    if not matching:
        raise KeyError(f"Proposal {proposal_id} not found in {dir_}.")
    path = matching[0]
    data = json.loads(path.read_text())
    entry = write(
        writer_id=approver_id,
        writer_tier=approver_tier,
        body=data.get("body", ""),
        project_code=project_code,
        skill_tags=tuple(data.get("skill_tags", [])),
        artifact_kind=data.get("artifact_kind", ""),
        capability_tags=tuple(data.get("capability_tags", [])),
        proposed_by=data.get("proposer_id"),
    )
    path.unlink()  # consumed
    return entry


def reject_proposal(proposal_id: str, *, project_code: str) -> bool:
    """QC or Leader rejects a proposal — file is removed without write."""
    dir_ = _team_dir(project_code) / "_proposals"
    if not dir_.exists():
        return False
    matching = list(dir_.glob(f"*{proposal_id}*.json"))
    if not matching:
        return False
    matching[0].unlink()
    return True


# ── Read API ──────────────────────────────────────────────────────────────

def list_entries(project_code: str) -> list[MemoryEntry]:
    """All approved team-memory entries in append order."""
    dir_ = _team_dir(project_code)
    if not dir_.exists():
        return []
    out: list[MemoryEntry] = []
    for f in sorted(dir_.glob("*.md")):
        rec = _parse(f)
        if rec is not None:
            out.append(rec)
    return out


# ── Targeted recall (load-bearing for pre-task consultation) ──────────────

def _cache_dir(project_code: str) -> Path:
    return config.get_cache_root() / "team-memory" / project_code.lower()


def _meta_path(project_code: str) -> Path:
    return _cache_dir(project_code) / "meta.json"


def _db_path(project_code: str) -> Path:
    return _cache_dir(project_code) / "lance.db"


def _config_hash(records: list[MemoryEntry]) -> str:
    ids = "||".join(r.entry_id for r in records)
    return hashlib.sha256((ids + f"|model={_embed_model()}").encode()).hexdigest()[:16]


def _load_meta(project_code: str) -> dict:
    p = _meta_path(project_code)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def _save_meta(project_code: str, meta: dict) -> None:
    _cache_dir(project_code).mkdir(parents=True, exist_ok=True)
    _meta_path(project_code).write_text(json.dumps(meta, indent=2))


def _ensure_vectors(project_code: str, embedder: Embedder) -> list[MemoryEntry]:
    """Build/refresh the LanceDB index from the markdown source. Returns
    the current record list.

    Serialized per project: the drop_table+create_table rebuild is destructive
    and must not interleave with another thread's rebuild or read.
    """
    with _vector_lock(project_code):
        records = list_entries(project_code)
        new_hash = _config_hash(records)
        meta = _load_meta(project_code)
        active_model = _embed_model()

        if (
            meta.get("records_hash") == new_hash
            and meta.get("embedding_model") == active_model
            and meta.get("embed_dim") == embedder.dim
        ):
            return records

        import lancedb
        import pyarrow as pa

        _cache_dir(project_code).mkdir(parents=True, exist_ok=True)
        db = lancedb.connect(_db_path(project_code))
        if "team_memory" in db.list_tables().tables:
            db.drop_table("team_memory")

        if not records:
            _save_meta(project_code, {
                "records_hash": new_hash,
                "embedding_model": active_model,
                "embed_dim": embedder.dim,
                "record_count": 0,
            })
            return records

        texts = [r.body for r in records]
        vectors = embedder.embed_texts(texts)
        rows = [{"vector": v, "entry_id": r.entry_id} for r, v in zip(records, vectors)]
        schema = pa.schema([
            pa.field("vector", pa.list_(pa.float32(), embedder.dim)),
            pa.field("entry_id", pa.string()),
        ])
        db.create_table("team_memory", data=rows, schema=schema)
        _save_meta(project_code, {
            "records_hash": new_hash,
            "embedding_model": active_model,
            "embed_dim": embedder.dim,
            "record_count": len(records),
        })
        return records


def recall(
    *,
    project_code: str,
    skill_names: list[str] | tuple[str, ...] = (),
    artifact_kind: str = "",
    capability_tags: list[str] | tuple[str, ...] = (),
    task_description: str = "",
    embedder: Optional[Embedder] = None,
    top_k: int = 5,
    min_similarity: float = 0.5,
) -> list[tuple[MemoryEntry, float | None]]:
    """Targeted retrieval — narrow by metadata, then top-K semantic.

    Each hit's score is the cosine similarity (float) when computed
    semantically, or ``None`` when the result came from a recency fallback
    (no embedder / no task_description / unavailable cache) — the None
    signals "unscored precedent" so callers don't mistake it for a perfect
    semantic match.

    Locked design (project_modulatio_v2_memory_architecture.md):
      1. Metadata pre-filter (skill_names ∩, artifact_kind exact, capability_tags ∩)
      2. Top-K semantic similarity within filtered subset against task_description
      3. min_similarity floor applied
      4. Cap K (default 5) — never returns full pool

    Without an embedder OR without task_description, falls back to
    metadata-filtered + recency-sorted. Test paths can run without
    embedder this way.
    """
    all_records = list_entries(project_code)
    if not all_records:
        return []

    skill_set = set(skill_names) if skill_names else set()
    cap_set = set(capability_tags) if capability_tags else set()

    def matches(r: MemoryEntry) -> bool:
        if skill_set and not (skill_set & set(r.skill_tags)):
            return False
        if artifact_kind and r.artifact_kind and r.artifact_kind != artifact_kind:
            return False
        if cap_set and not (cap_set & set(r.capability_tags)):
            return False
        return True

    filtered = [r for r in all_records if matches(r)]
    if not filtered:
        return []

    # Without embedder or task description: recency-sort filtered subset, take top-K.
    # These hits are NOT semantically scored, so similarity is None (a distinct
    # "unscored / recency" sentinel) rather than a fabricated 1.0 — a 1.0 would
    # falsely claim perfect relevance, bypass min_similarity, and mislead the
    # producer. render_for_prompt surfaces None as "recency".
    if embedder is None or not task_description:
        filtered.sort(key=lambda r: r.timestamp, reverse=True)
        return [(r, None) for r in filtered[:top_k]]

    # Semantic retrieval within filtered subset. Hold the per-project lock
    # across the rebuild AND the read so no concurrent wave worker drops/
    # recreates the shared table mid-search (the lock is reentrant, so the
    # nested _ensure_vectors acquire is free).
    import lancedb

    def _recency_fallback() -> list[tuple[MemoryEntry, float | None]]:
        # Markdown is the source of truth; LanceDB is a rebuildable cache.
        # When the cache is unavailable (missing path / table / empty table)
        # but the metadata-filtered markdown set is non-empty, surface those
        # entries by recency rather than dropping real precedent on the floor.
        # similarity is None (unscored) — no semantic score was computed, so we
        # must not label them sim 1.00 and bypass the min_similarity contract.
        ranked = sorted(filtered, key=lambda r: r.timestamp, reverse=True)
        return [(r, None) for r in ranked[:top_k]]

    with _vector_lock(project_code):
        _ensure_vectors(project_code, embedder)

        path = _db_path(project_code)
        if not path.exists():
            return _recency_fallback()

        db = lancedb.connect(path)
        if "team_memory" not in db.list_tables().tables:
            return _recency_fallback()
        table = db.open_table("team_memory")
        if table.count_rows() == 0:
            return _recency_fallback()

        filtered_ids = {r.entry_id for r in filtered}
        by_id = {r.entry_id: r for r in filtered}

        query_vec = embedder.embed_text(task_description)
        # Pull more than top_k from LanceDB so the post-filter has slack.
        raw_results = (
            table.search(query_vec)
            .metric("cosine")
            .limit(top_k * 4)
            .to_list()
        )

        hits: list[tuple[MemoryEntry, float]] = []
        for row in raw_results:
            eid = row.get("entry_id")
            if eid not in filtered_ids:
                continue
            rec = by_id.get(eid)
            if rec is None:
                continue
            distance = float(row.get("_distance", 1.0))
            similarity = 1.0 - distance
            if similarity < min_similarity:
                continue
            hits.append((rec, similarity))
            if len(hits) >= top_k:
                break
        return hits


def render_for_prompt(hits: list[tuple[MemoryEntry, float | None]]) -> str:
    """Format a recall result for injection into a producer prompt's
    ``{team_memory_context}`` slot.

    Empty hits → neutral marker so producers don't see a blank slot.
    A ``None`` score (recency fallback — no semantic score was computed) is
    rendered as ``recency`` rather than a fabricated ``sim 1.00``."""
    if not hits:
        return "(no team-memory precedent for this task)"
    lines = []
    for rec, sim in hits:
        author = rec.writer_id
        if rec.proposed_by and rec.proposed_by != rec.writer_id:
            author = f"{rec.proposed_by} (approved by {rec.writer_id})"
        score = "recency" if sim is None else f"sim {sim:.2f}"
        lines.append(
            f"- [{rec.entry_id} | {rec.artifact_kind or '?'} | {score} | by {author}] {rec.body.strip()[:300]}"
        )
    return "\n".join(lines)


__all__ = [
    "MemoryEntry",
    "Proposal",
    "PermissionDenied",
    "write",
    "propose",
    "list_proposals",
    "approve_proposal",
    "reject_proposal",
    "list_entries",
    "recall",
    "render_for_prompt",
]
