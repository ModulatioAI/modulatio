# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Per-agent private memory — carried from v1.3.1 ``agent_memory.py``.

Each agent has:
- Episodic memory: recent entries with full context (auto-decays)
- Semantic memory: promoted long-term facts (curated, durable)

Strict per-agent isolation — only the owning agent reads or writes.
Cross-agent sharing happens via ``team_memory.propose()`` (Slice 4).

Storage:
    <vault>/<code>/memory/<agent_id>/episodic.json
    <vault>/<code>/memory/<agent_id>/semantic.json

v1.3 differences:
- Per-project paths (rather than v1.3's per-install)
- Dropped crew_memory vector mirror (team_memory replaces with QC-curated pool)
- Dropped auto-promotion to global (handled by explicit team_memory.propose)
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from modulatio.vault import project_dir, validate_registry_name


# Tunables (carried from v1.3.1; keep conservative — small per-agent state).
EPISODIC_STALE_DAYS = 14
EPISODIC_MAX_ENTRIES = 100
SEMANTIC_MAX_ENTRIES = 50


# === Path helpers ===

def _agent_dir(agent_id: str, project_code: str) -> Path:
    # H1 invariant (mirror skills/standards/job-templates): agent_id becomes a
    # path component, so a separator / '..' / leading dot must never escape the
    # project's memory/ root. Fail-closed — the persistence layer binds it.
    proj = project_dir(project_code)
    base = proj / "memory"
    d = base / validate_registry_name(agent_id)
    # The name is validated, but a pre-planted SYMLINK at the agent dir OR at the
    # memory/ root would still be followed by mkdir(exist_ok=True) + the writes —
    # an escape. Refuse a symlinked memory/ root and a symlinked agent dir, and
    # bounds-check against the REAL project root (NOT base.resolve(), which a
    # symlinked memory/ would bless to its own outside target). Mirror
    # vault.run_dir's belt-and-suspenders so reads/writes never leave the project.
    if base.is_symlink():
        raise ValueError(
            "project memory root is a symlink — refusing to follow it")
    if d.is_symlink():
        raise ValueError(
            f"agent memory dir {agent_id!r} is a symlink — refusing to follow it")
    try:
        d.resolve(strict=False).relative_to(proj.resolve(strict=False))
    except ValueError as exc:
        raise ValueError(
            f"agent memory dir {agent_id!r} escapes the project root"
        ) from exc
    d.mkdir(parents=True, exist_ok=True)
    return d


def _episodic_path(agent_id: str, project_code: str) -> Path:
    return _agent_dir(agent_id, project_code) / "episodic.json"


def _semantic_path(agent_id: str, project_code: str) -> Path:
    return _agent_dir(agent_id, project_code) / "semantic.json"


def _load_json(path: Path) -> list:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace")) or []
    except (OSError, json.JSONDecodeError):
        return []


def _save_json(path: Path, data: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # atomic, crash-safe write (mirror of store._write_entity). A plain
    # path.write_text truncates-then-streams, so the lock-free readers (get_semantic,
    # search, stats, promote_candidates — they don't take _file_lock) can observe a
    # half-written file mid-rewrite; _load_json then swallows the JSONDecodeError and
    # returns [] — a silent whole-store loss on every torn read. A crash mid-write
    # would likewise leave the live store truncated. Write to a unique temp sibling,
    # fsync, then os.replace (atomic rename on the same filesystem): a reader always
    # sees either the complete old file or the complete new one, never torn, and a
    # crash can never truncate the live store.
    rendered = json.dumps(data, indent=2, default=str)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(rendered)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# Per-file read-modify-write lock. add_episodic/get_episodic/add_semantic/
# decay_episodic all load the whole JSON, mutate, and rewrite the entire file;
# without a lock two concurrent operations on the same (agent_id, project_code)
# — e.g. a chat turn appending while a parallel wave worker bumps access_count —
# can lose updates (last writer wins on the full-file rewrite). Mirror of
# team_memory._vector_lock: a per-key reentrant threading lock (a plain
# in-process lock, no fork interaction). Keyed by the resolved file path so each
# episodic/semantic store serializes independently and unrelated stores never
# contend. Reentrant so a future caller can nest helpers under one hold.
_FILE_LOCKS: dict[str, threading.RLock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _file_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _FILE_LOCKS[key] = lock
        return lock


# === Entry dataclass ===

@dataclass
class MemoryEntry:
    """A single agent-memory entry. Mirrors v1.3.1 schema."""

    id: str
    content: str
    type: str  # observation | decision | finding | preference | contact | task
    source: str  # crew_run | chat | user | promotion
    when: str  # ISO timestamp
    confidence: str  # high | med | low
    scope: str  # global | project | agent | temporary
    state: str  # active | stale | superseded | archived
    tags: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    access_count: int = 0
    last_accessed: Optional[str] = None
    created: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryEntry":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Monotonic suffix to break same-window id ties. The %f timestamp truncates to
# ~100µs resolution, so concurrent _create_entry calls (e.g. parallel wave
# workers writing per-agent memory) landing in the same window would otherwise
# collide and silently overwrite each other. Mirror of team_memory._new_id().
_ID_LOCK = threading.Lock()
_ID_COUNTER = 0


def _new_id() -> str:
    global _ID_COUNTER
    prefix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")[:18]
    with _ID_LOCK:
        _ID_COUNTER = (_ID_COUNTER + 1) % 1000000
        seq = _ID_COUNTER
    return f"{prefix}{seq:06d}"


def _create_entry(
    content: str,
    *,
    source: str = "crew_run",
    entry_type: str = "observation",
    confidence: str = "med",
    scope: str = "project",
    tags: Optional[list[str]] = None,
    related: Optional[list[str]] = None,
) -> MemoryEntry:
    now = _now_iso()
    return MemoryEntry(
        id=_new_id(),
        content=content,
        type=entry_type,
        source=source,
        when=now,
        confidence=confidence,
        scope=scope,
        state="active",
        tags=list(tags or []),
        related=list(related or []),
        access_count=0,
        last_accessed=None,
        created=now,
    )


# === Episodic memory ===

def add_episodic(
    agent_id: str,
    content: str,
    *,
    project_code: str,
    source: str = "crew_run",
    entry_type: str = "observation",
    confidence: str = "med",
    tags: Optional[list[str]] = None,
) -> MemoryEntry:
    """Add an episodic memory entry. Auto-prunes when over EPISODIC_MAX_ENTRIES."""
    path = _episodic_path(agent_id, project_code)
    entry = _create_entry(content, source=source, entry_type=entry_type, confidence=confidence, tags=tags)
    with _file_lock(path):
        entries = _load_json(path)
        entries.append(entry.to_dict())
        if len(entries) > EPISODIC_MAX_ENTRIES:
            entries = entries[-EPISODIC_MAX_ENTRIES:]
        _save_json(path, entries)
    return entry


def get_episodic(
    agent_id: str,
    *,
    project_code: str,
    limit: int = 20,
    tags: Optional[list[str]] = None,
    active_only: bool = True,
) -> list[MemoryEntry]:
    """Retrieve recent episodic memories. Updates access_count + last_accessed."""
    path = _episodic_path(agent_id, project_code)
    with _file_lock(path):
        raw = _load_json(path)
        entries = [MemoryEntry.from_dict(e) for e in raw]
        if active_only:
            entries = [e for e in entries if e.state == "active"]
        if tags:
            entries = [e for e in entries if any(t in e.tags for t in tags)]
        result = entries[-limit:]
        if result:
            accessed_ids = {e.id for e in result}
            now = _now_iso()
            for e in raw:
                if e.get("id") in accessed_ids:
                    e["access_count"] = int(e.get("access_count") or 0) + 1
                    e["last_accessed"] = now
            _save_json(path, raw)
    return result


# === Semantic memory ===

def add_semantic(
    agent_id: str,
    content: str,
    *,
    project_code: str,
    entry_type: str = "finding",
    confidence: str = "high",
    scope: str = "project",
    tags: Optional[list[str]] = None,
    supersedes: Optional[str] = None,
) -> MemoryEntry:
    """Add a semantic (long-term) memory entry. Optionally supersedes an
    earlier entry by content fragment match."""
    path = _semantic_path(agent_id, project_code)
    entry = _create_entry(content, source="promotion", entry_type=entry_type, confidence=confidence, scope=scope, tags=tags)
    with _file_lock(path):
        entries = _load_json(path)
        if supersedes:
            for e in entries:
                if supersedes.lower() in (e.get("content") or "").lower():
                    e["state"] = "superseded"
        entries.append(entry.to_dict())
        if len(entries) > SEMANTIC_MAX_ENTRIES:
            active = [e for e in entries if e.get("state") == "active"]
            inactive = [e for e in entries if e.get("state") != "active"]
            # Keep a slice of inactive (audit tail) plus the freshest active, but
            # never exceed SEMANTIC_MAX_ENTRIES overall: when active alone exceeds
            # the cap, inactive[-MAX//5:] + active[-MAX:] would overshoot, so cap
            # the combined result. Active is favored (kept last → survives the
            # final trim) over the inactive audit tail.
            entries = (inactive[-(SEMANTIC_MAX_ENTRIES // 5):] + active)[-SEMANTIC_MAX_ENTRIES:]
        _save_json(path, entries)
    return entry


def get_semantic(
    agent_id: str,
    *,
    project_code: str,
    limit: int = 20,
    tags: Optional[list[str]] = None,
) -> list[MemoryEntry]:
    """Retrieve active semantic memories."""
    raw = _load_json(_semantic_path(agent_id, project_code))
    entries = [MemoryEntry.from_dict(e) for e in raw if e.get("state") == "active"]
    if tags:
        entries = [e for e in entries if any(t in e.tags for t in tags)]
    return entries[-limit:]


# === Search + maintenance ===

def search(
    agent_id: str,
    query: str,
    *,
    project_code: str,
    limit: int = 10,
) -> list[MemoryEntry]:
    """Simple keyword search across both episodic and semantic stores."""
    q = query.lower()
    out: list[MemoryEntry] = []
    for raw in _load_json(_semantic_path(agent_id, project_code)):
        if q in (raw.get("content") or "").lower():
            out.append(MemoryEntry.from_dict(raw))
    for raw in _load_json(_episodic_path(agent_id, project_code)):
        if q in (raw.get("content") or "").lower():
            out.append(MemoryEntry.from_dict(raw))
    out.sort(key=lambda e: e.when, reverse=True)
    return out[:limit]


def decay_episodic(agent_id: str, *, project_code: str) -> int:
    """Mark episodic entries older than EPISODIC_STALE_DAYS as stale.
    Returns count of newly-staled entries."""
    path = _episodic_path(agent_id, project_code)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=EPISODIC_STALE_DAYS)).isoformat(timespec="seconds")
    with _file_lock(path):
        entries = _load_json(path)
        staled = 0
        for e in entries:
            if e.get("state") == "active" and (e.get("when") or "") < cutoff:
                e["state"] = "stale"
                staled += 1
        if staled:
            _save_json(path, entries)
    return staled


def promote_candidates(agent_id: str, *, project_code: str) -> list[MemoryEntry]:
    """Find episodic entries worth promoting to semantic memory.

    Heuristic (carried from v1.3.1):
      - high confidence + access_count >= 2, OR
      - type in (decision, preference, contact)
    """
    raw = _load_json(_episodic_path(agent_id, project_code))
    candidates: list[MemoryEntry] = []
    seen_ids: set[str] = set()
    for e in raw:
        if e.get("state") != "active":
            continue
        eid = e.get("id")
        if not eid or eid in seen_ids:
            continue
        if (e.get("confidence") == "high" and int(e.get("access_count") or 0) >= 2) or e.get("type") in ("decision", "preference", "contact"):
            candidates.append(MemoryEntry.from_dict(e))
            seen_ids.add(eid)
    return candidates


def _layer_path(agent_id: str, layer: str, project_code: str) -> Path:
    """Resolve the JSON path for a layer ('episodic' | 'semantic'). Raises
    ValueError for any other layer (the team layer is QC-curated and is never
    mutated through this private-memory module)."""
    if layer == "episodic":
        return _episodic_path(agent_id, project_code)
    if layer == "semantic":
        return _semantic_path(agent_id, project_code)
    raise ValueError(
        f"unknown memory layer {layer!r} (expected 'episodic' or 'semantic')")


def delete_entry(
    agent_id: str, entry_id: str, *, project_code: str, layer: str,
) -> bool:
    """Delete one episodic/semantic entry by id. Returns True if it existed.

    The agent_id is path-validated by ``_layer_path`` (via ``_agent_dir``), so a
    traversal id fails closed. Mirrors ``decay_episodic``'s load-mutate-save
    under the per-file lock."""
    path = _layer_path(agent_id, layer, project_code)
    with _file_lock(path):
        entries = _load_json(path)
        kept = [e for e in entries if e.get("id") != entry_id]
        if len(kept) == len(entries):
            return False
        _save_json(path, kept)
    return True


def update_entry(
    agent_id: str, entry_id: str, *, project_code: str, layer: str, content: str,
) -> Optional[MemoryEntry]:
    """Edit one episodic/semantic entry's content in place (same id). Returns the
    updated entry, or None if no entry matched."""
    path = _layer_path(agent_id, layer, project_code)
    with _file_lock(path):
        entries = _load_json(path)
        found = None
        for e in entries:
            if e.get("id") == entry_id:
                e["content"] = content
                found = e
                break
        if found is None:
            return None
        _save_json(path, entries)
    return MemoryEntry.from_dict(found)


def export_markdown(agent_id: str, *, project_code: str) -> str:
    """Render an agent's episodic + semantic memory as markdown. Read-only: reads the raw JSON directly so it
    never bumps access bookkeeping the way ``get_episodic`` does."""
    def _section(title: str, path: Path) -> list[str]:
        rows = [MemoryEntry.from_dict(e) for e in _load_json(path)]
        out = [f"## {title}", ""]
        if not rows:
            out += ["_(none)_", ""]
            return out
        for e in rows:
            stamp = (e.when or "")[:19]
            meta = " · ".join(p for p in (e.type, e.confidence, e.state) if p)
            out.append(f"- **{stamp}** ({meta}) {e.content}")
        out.append("")
        return out

    lines = [f"# Memory · {agent_id}", ""]
    lines += _section("Episodic", _episodic_path(agent_id, project_code))
    lines += _section("Semantic", _semantic_path(agent_id, project_code))
    return "\n".join(lines)


def stats(agent_id: str, *, project_code: str) -> dict:
    """Memory stats for an agent."""
    episodic = _load_json(_episodic_path(agent_id, project_code))
    semantic = _load_json(_semantic_path(agent_id, project_code))
    return {
        "episodic_total": len(episodic),
        "episodic_active": sum(1 for e in episodic if e.get("state") == "active"),
        "episodic_stale": sum(1 for e in episodic if e.get("state") == "stale"),
        "semantic_total": len(semantic),
        "semantic_active": sum(1 for e in semantic if e.get("state") == "active"),
    }


__all__ = [
    "MemoryEntry",
    "add_episodic",
    "get_episodic",
    "add_semantic",
    "get_semantic",
    "search",
    "delete_entry",
    "update_entry",
    "export_markdown",
    "decay_episodic",
    "promote_candidates",
    "stats",
    "EPISODIC_STALE_DAYS",
    "EPISODIC_MAX_ENTRIES",
    "SEMANTIC_MAX_ENTRIES",
]
