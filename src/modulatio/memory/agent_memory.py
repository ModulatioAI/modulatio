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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from modulatio.vault import project_dir


# Tunables (carried from v1.3.1; keep conservative — small per-agent state).
EPISODIC_STALE_DAYS = 14
EPISODIC_MAX_ENTRIES = 100
SEMANTIC_MAX_ENTRIES = 50


# === Path helpers ===

def _agent_dir(agent_id: str, project_code: str) -> Path:
    d = project_dir(project_code) / "memory" / agent_id
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
        return json.loads(path.read_text()) or []
    except (OSError, json.JSONDecodeError):
        return []


def _save_json(path: Path, data: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


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


def _new_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")[:18]


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
    entries = _load_json(path)
    entry = _create_entry(content, source=source, entry_type=entry_type, confidence=confidence, tags=tags)
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
    entries = _load_json(path)
    if supersedes:
        for e in entries:
            if supersedes.lower() in (e.get("content") or "").lower():
                e["state"] = "superseded"
    entry = _create_entry(content, source="promotion", entry_type=entry_type, confidence=confidence, scope=scope, tags=tags)
    entries.append(entry.to_dict())
    if len(entries) > SEMANTIC_MAX_ENTRIES:
        active = [e for e in entries if e.get("state") == "active"]
        inactive = [e for e in entries if e.get("state") != "active"]
        entries = inactive[-(SEMANTIC_MAX_ENTRIES // 5):] + active[-SEMANTIC_MAX_ENTRIES:]
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
    entries = _load_json(path)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=EPISODIC_STALE_DAYS)).isoformat(timespec="seconds")
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
    "decay_episodic",
    "promote_candidates",
    "stats",
    "EPISODIC_STALE_DAYS",
    "EPISODIC_MAX_ENTRIES",
    "SEMANTIC_MAX_ENTRIES",
]
