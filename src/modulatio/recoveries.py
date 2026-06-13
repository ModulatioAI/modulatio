# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""The recovery feed — the win-codification source (#81 / Fix F).

The fail-loop (:mod:`modulatio.lessons`) learns from what QC REJECTED. This module
is its symmetric twin: it learns from what QC FIXED. When the smart QC rescues a
cheap producer — writing the patch the producer couldn't (``_qc_patch_artifact``) —
that patch encodes a TECHNIQUE the cheap producer lacked. We witness it as a
:class:`RecoveryRecord` (the before/defects/after triple, bounded), and the win
loop clusters recoveries behind an ENGINE recurrence floor so the Leader codifies a
recurring technique, not a one-off incident.

Two disciplines are load-bearing (both from the #81 hull review):

* **Write-time truncation (Nemo #6).** Every text field is clipped to
  ``MAX_RECOVERY_EXCERPT_CHARS`` *inside* :func:`record_recovery`, regardless of the
  caller — the technique lives in the delta, and a 300k-token artifact must not land
  in the log.
* **A false-merge-resistant signature (Hero R3).** Two recoveries cluster only when
  BOTH their rationale-key AND their artifact-kind-aware change-shape agree. Anything
  the engine cannot cheaply classify gets a UNIQUE ``unclassified:<id>`` change-shape
  — a permanent singleton that can never reach the floor (fail-open to *split*, never
  a wrong generalization merged into a shared skill).

This module is a pure witness + a cheap deterministic clusterer. It NEVER fails a
task — the orchestration call site guards :func:`record_recovery` and the loop is
best-effort. Judgment (which cluster is one teachable technique) stays with the
Leader; the engine only binds recurrence.
"""

from __future__ import annotations

import difflib
import json
import re
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

from modulatio.vault import project_dir

#: Hard write-time cap on every stored text field (Nemo #6) — chars, not tokens.
MAX_RECOVERY_EXCERPT_CHARS = 2000

#: How many significant rationale tokens make the lexical half of the signature.
_RATIONALE_KEY_TOKENS = 8

_STOPWORDS = frozenset(
    "a an the is are was were be been being to of in on for and or but with without "
    "this that these those it its as at by from into out over under no not".split()
)

#: Token families a code change-shape distinguishes (Hero R3).
_CTRL_KEYWORDS = frozenset(
    "if elif else for while try except finally with return raise break continue "
    "yield match case assert".split()
)
#: kinds whose change-shape is categorized by the CODE categorizer.
_CODE_KIND_TOKENS = frozenset(
    "code py python js javascript ts typescript java go rust c cpp script module "
    "program source".split()
)
_DOC_KIND_TOKENS = frozenset(
    "doc document essay article prose report chapter story poem letter markdown "
    "md text".split()
)
_DATA_KIND_TOKENS = frozenset(
    "data dataset csv tsv table json yaml records rows".split()
)


# ── the record ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RecoveryRecord:
    """A witnessed QC recovery — the teaching triple, bounded. ``signature`` is the
    deterministic cluster key (computed at write time)."""

    # Every field carries a default (#81): a future schema addition must not make
    # historical lines (which lack the new key) raise TypeError on load and silently
    # drop the ENTIRE recovery feed. Defaults + known-key filtering in
    # load_recoveries make the record forward- AND backward-compatible.
    entry_id: str = ""
    timestamp: str = ""
    kind: str = ""           # "qc_authored" | "redo_guided"
    artifact_kind: str = ""
    defect_type: str = ""    # "mechanical" | "substantive" | "environmental" | ""
    task_id: str = ""
    defects: str = ""
    before_excerpt: str = ""
    after_excerpt: str = ""
    qc_rationale: str = ""
    signature: str = ""


def _truncate(s: object) -> str:
    return str(s or "")[:MAX_RECOVERY_EXCERPT_CHARS]


# ── change-shape fingerprint (artifact-kind-aware, Hero R3) ────────────────────


def _kind_tokens(artifact_kind: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (artifact_kind or "").lower()) if t]


def _band(n: int) -> str:
    """Coarse magnitude band — keeps the fingerprint stable across trivial size noise."""
    if n == 0:
        return "0"
    if n <= 2:
        return "S"
    if n <= 8:
        return "M"
    return "L"


def _sign(n: int) -> str:
    return "+" if n > 0 else ("-" if n < 0 else "0")


def _diff_lines(before: str, after: str) -> "tuple[list[str], list[str]]":
    b = str(before).splitlines()
    a = str(after).splitlines()
    diff = list(difflib.unified_diff(b, a, lineterm=""))
    added = [ln[1:] for ln in diff if ln.startswith("+") and not ln.startswith("+++")]
    removed = [ln[1:] for ln in diff if ln.startswith("-") and not ln.startswith("---")]
    return added, removed


def _count_ctrl(lines: "list[str]") -> int:
    return sum(
        1
        for ln in lines
        for tok in re.split(r"[^a-zA-Z_]+", ln)
        if tok in _CTRL_KEYWORDS
    )


def _count_literals(lines: "list[str]") -> int:
    return sum(len(re.findall(r"'[^']*'|\"[^\"]*\"|\b\d+\b", ln)) for ln in lines)


def _count_identifiers(lines: "list[str]") -> int:
    """Identifier-like tokens (names) in the diff lines, EXCLUDING control-flow
    keywords (counted separately as ``ctrl``). The third token class the design
    pins (Hero MINOR 4 — omitting it coarsens the signature toward false-merge)."""
    return sum(
        1
        for ln in lines
        for tok in re.findall(r"[A-Za-z_]\w*", ln)
        if tok not in _CTRL_KEYWORDS
    )


def _code_shape(before: str, after: str) -> str:
    added, removed = _diff_lines(before, after)
    ctrl = _count_ctrl(added) - _count_ctrl(removed)
    lit = _count_literals(added) - _count_literals(removed)
    idn = _count_identifiers(added) - _count_identifiers(removed)
    return (
        f"code:add={_band(len(added))}:rm={_band(len(removed))}"
        f":ctrl={_sign(ctrl)}:lit={_sign(lit)}:id={_sign(idn)}"
    )


def _sentences(text: str) -> int:
    return len([s for s in re.split(r"[.!?]+", str(text)) if s.strip()])


def _headings(text: str) -> int:
    return len([ln for ln in str(text).splitlines() if ln.lstrip().startswith("#")])


def _doc_shape(before: str, after: str) -> str:
    sent = _sentences(after) - _sentences(before)
    head = _headings(after) - _headings(before)
    return f"doc:sent={_band(abs(sent))}:dir={_sign(sent)}:head={_sign(head)}"


def _data_shape(before: str, after: str) -> str:
    rows = str(after).count("\n") - str(before).count("\n")
    cols = str(after).split("\n")[0].count(",") - str(before).split("\n")[0].count(",")
    return f"data:rows={_band(abs(rows))}:dir={_sign(rows)}:cols={_sign(cols)}"


def change_shape(before: object, after: object, artifact_kind: str) -> "str | None":
    """A deterministic, artifact-kind-aware fingerprint of the before→after delta, or
    ``None`` when the engine cannot cheaply classify the kind (the caller then
    substitutes a unique singleton sentinel). No LLM. Total — any failure → None."""
    toks = _kind_tokens(artifact_kind)
    try:
        b, a = str(before), str(after)
        # A no-delta "recovery" taught nothing (empty→empty, or before == after):
        # there is no technique to fingerprint, so fail open to a unique singleton
        # (#80) rather than emit a stable shape that could false-merge across tasks.
        if b == a:
            return None
        if any(t in _CODE_KIND_TOKENS for t in toks):
            return _code_shape(b, a)
        if any(t in _DOC_KIND_TOKENS for t in toks):
            return _doc_shape(b, a)
        if any(t in _DATA_KIND_TOKENS for t in toks):
            return _data_shape(b, a)
    except Exception:  # noqa: BLE001 — total: an unclassifiable delta → singleton
        return None
    return None


# ── the recovery signature (the cluster key) ──────────────────────────────────


def _rationale_key(rationale: str) -> str:
    toks = [
        t
        for t in re.split(r"[^a-z0-9]+", str(rationale).lower())
        if t and t not in _STOPWORDS
    ]
    return "-".join(toks[:_RATIONALE_KEY_TOKENS])


def recovery_signature(
    artifact_kind: str, defect_type: str, rationale: str, change_shape_str: str
) -> str:
    """``(artifact_kind, defect_type, rationale-key, change-shape)`` — two recoveries
    cluster only when ALL FOUR agree. Biased to false-split (safe) over false-merge.

    Commas are stripped from every component (Hero note): the signature round-trips
    through the skill's ``learned_from`` frontmatter as a COMMA-separated list, so a
    comma inside a component would split the entry and silently disarm the replay guard
    for that skill."""
    def _safe(s: str) -> str:
        return str(s or "").strip().lower().replace(",", ";")

    return "|".join([
        _safe(artifact_kind),
        _safe(defect_type),
        _safe(_rationale_key(rationale)),
        _safe(change_shape_str),
    ])


# ── witness + storage ─────────────────────────────────────────────────────────


def _log_path(project_code: str) -> Path:
    return project_dir(project_code) / "recoveries" / "log.jsonl"


def _consumed_path(project_code: str) -> Path:
    # SEPARATE from lessons/_consumed (Nemo #2) — a recovery id must never be
    # consumed against the fail ledger's namespace.
    return project_dir(project_code) / "recoveries" / "_consumed_recoveries"


def record_recovery(
    project_code: str,
    *,
    kind: str,
    artifact_kind: str,
    defect_type: str,
    task_id: str,
    defects: str,
    before: str,
    after: str,
    qc_rationale: str,
    entry_id: "str | None" = None,
    timestamp: "str | None" = None,
) -> RecoveryRecord:
    """Witness a QC recovery. Truncates every text field at write time (Nemo #6),
    computes the false-merge-resistant signature (Hero R3 — unclassifiable kind →
    a unique ``unclassified:<id>`` singleton), appends one JSON line, returns the
    record. The caller guards this; it is pure upside capture."""
    eid = entry_id or uuid.uuid4().hex
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    # Truncate EVERY text field up front, and compute the signature from the TRUNCATED
    # rationale (Nemo code #2 — the signature's rationale-key must derive from the
    # bounded stored text, not the raw input that could carry a sentinel past the cap).
    before_x = _truncate(before)
    after_x = _truncate(after)
    defects_x = _truncate(defects)
    qc_rationale_x = _truncate(qc_rationale)
    shape = change_shape(before_x, after_x, artifact_kind)
    if shape is None:
        shape = f"unclassified:{eid}"  # permanent singleton — can never false-merge
    rec = RecoveryRecord(
        entry_id=eid,
        timestamp=ts,
        kind=str(kind),
        artifact_kind=str(artifact_kind or ""),
        defect_type=str(defect_type or ""),
        task_id=str(task_id or ""),
        defects=defects_x,
        before_excerpt=before_x,
        after_excerpt=after_x,
        qc_rationale=qc_rationale_x,
        signature=recovery_signature(artifact_kind, defect_type, qc_rationale_x, shape),
    )
    p = _log_path(project_code)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(asdict(rec)) + "\n")
    return rec


def load_recoveries(project_code: str) -> "list[RecoveryRecord]":
    p = _log_path(project_code)
    if not p.exists():
        return []
    out: list[RecoveryRecord] = []
    known = {f.name for f in fields(RecoveryRecord)}
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    continue
                # Drop only UNKNOWN keys (a since-removed schema field); missing keys
                # fall back to field defaults (#81). The historical feed survives any
                # schema add/remove instead of being wholesale discarded.
                out.append(RecoveryRecord(**{k: v for k, v in obj.items() if k in known}))
            except (ValueError, TypeError):
                continue  # a malformed line never blocks the feed
    except OSError:
        return []
    return out


# ── consumed ledger (mirrors lessons, separate file) ──────────────────────────


def consumed_ids(project_code: str) -> "set[str]":
    p = _consumed_path(project_code)
    if not p.exists():
        return set()
    try:
        return {ln.strip() for ln in p.read_text().splitlines() if ln.strip()}
    except OSError:
        return set()


def mark_consumed(project_code: str, entry_ids) -> None:
    ids = [e for e in entry_ids if e]
    if not ids:
        return
    p = _consumed_path(project_code)
    p.parent.mkdir(parents=True, exist_ok=True)
    have = consumed_ids(project_code)
    new = [e for e in ids if e not in have]
    if new:
        with p.open("a") as f:
            for e in new:
                f.write(e + "\n")


def unconsumed_recoveries(project_code: str, limit: int = 30) -> "list[RecoveryRecord]":
    """Un-consumed recoveries, most-recent first, capped — the raw material the win
    loop clusters + the Leader judges."""
    consumed = consumed_ids(project_code)
    rows = [r for r in load_recoveries(project_code) if r.entry_id not in consumed]
    rows.sort(key=lambda r: r.timestamp, reverse=True)
    return rows[: max(1, limit)]


# ── the engine recurrence floor ───────────────────────────────────────────────


def cluster_recoveries(
    records: "list[RecoveryRecord]", floor: int = 3
) -> "list[list[RecoveryRecord]]":
    """Group recoveries by signature; return ONLY clusters with ``>= floor`` members
    (the engine binds recurrence). An ``unclassified:<id>`` signature is unique per
    record, so it is a permanent singleton and can never reach the floor — the
    fail-open-to-split default (Hero R3). The Leader judges coherence *within* a
    surfaced cluster; the engine never claims a one-off is a technique."""
    by_sig: dict[str, list[RecoveryRecord]] = {}
    for r in records:
        by_sig.setdefault(r.signature, []).append(r)
    return [grp for grp in by_sig.values() if len(grp) >= max(1, floor)]


__all__ = [
    "MAX_RECOVERY_EXCERPT_CHARS",
    "RecoveryRecord",
    "change_shape",
    "recovery_signature",
    "record_recovery",
    "load_recoveries",
    "consumed_ids",
    "mark_consumed",
    "unconsumed_recoveries",
    "cluster_recoveries",
    "project_dir",
]
