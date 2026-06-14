# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Standards proposals — QC's side-channel for suggesting new team rules (slice #10).

When QC sees a recurring pattern across prior verdicts (via #8.1's
embedded history retrieval) that isn't already captured in the
standards, it may emit a ``proposed_standard`` alongside its normal
verdict. The orchestrator persists each proposal as a markdown file
under ``<project_vault>/standards-proposals/``. A human reviews via
the ``modulatio-standards`` CLI: approve appends the rule body to
``<project_vault>/standards/<domain>.md`` under a single
``## Team-approved rules`` header; reject deletes the proposal.

First human-in-loop approval flow in Modulatio. Business-harness
level: proposals carry a free-form ``domain`` string (``text``,
``code``, ``research``, or whatever the team uses) and a plain rule
body. No product-specific vocabulary leaks into the mechanism; the
rule text itself is whatever QC wrote about the team's actual work.

Write-side of the Research-First pattern that slices #2/#3 shipped
the read-side of — project standards now grow from observed
practice, not just hand-curated defaults.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from modulatio.vault import project_dir, validate_registry_name


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_SLUG_SAFE_RE = re.compile(r"[^a-z0-9]+")
_TEAM_HEADER = "## Team-approved rules"


@dataclass(frozen=True)
class Proposal:
    """A pending QC-proposed standards rule awaiting human review.

    - ``domain``: which ``standards/<domain>.md`` the approved rule
      would append to. Free-form; tracks the task's ``artifact_kind``.
    - ``title``: short human-readable heading for the rule (first
      line of the approved entry).
    - ``rule_body``: the actual rule text as it should appear in the
      domain standards file.
    - ``evidence_refs``: ids (or any identifiers) of the QC-history
      entries that motivated this proposal. Audit trail.
    - ``rationale``: one-line explanation of why QC proposed this.
    """

    domain: str
    title: str
    rule_body: str
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    rationale: str = ""


def _proposals_dir(project_code: str) -> Path:
    return project_dir(project_code) / "standards-proposals"


def _standards_dir(project_code: str) -> Path:
    return project_dir(project_code) / "standards"


def _slug(text: str) -> str:
    slug = _SLUG_SAFE_RE.sub("-", text.lower()).strip("-")
    return slug[:60] or "proposal"


def _proposal_id(now: datetime, title: str) -> str:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}__{_slug(title)}"


def _fm_safe(value: str) -> str:
    """Collapse newlines/carriage returns in a value before it is interpolated
    into a frontmatter line, so an attacker-controlled field (domain / title /
    rationale from a producer's proposal) cannot inject its own YAML keys —
    e.g. a title of ``x\\ndomain: ../../etc`` overriding the real domain on
    re-parse, which then steers the approve()-time write path."""
    return " ".join(str(value).splitlines()).strip()


def save(proposal: Proposal, project_code: str) -> Path:
    """Persist a proposal to ``<vault>/standards-proposals/<id>.md``.

    Returns the written path — the caller uses ``path.stem`` as the
    proposal id for later approve/reject.
    """
    root = _proposals_dir(project_code)
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    pid = _proposal_id(now, proposal.title)
    path = root / f"{pid}.md"
    # Same-second + same-title proposals would otherwise collide on the id and
    # silently overwrite the earlier one. Append a short disambiguating suffix
    # so each proposal keeps a distinct file (and a distinct stem-as-id).
    if path.exists():
        seq = 1
        while True:
            candidate = root / f"{pid}-{seq}.md"
            if not candidate.exists():
                path = candidate
                break
            seq += 1
    evidence_line = ", ".join(_fm_safe(r) for r in proposal.evidence_refs)
    content = (
        f"---\n"
        f"domain: {_fm_safe(proposal.domain)}\n"
        f"title: {_fm_safe(proposal.title)}\n"
        f"evidence_refs: {evidence_line}\n"
        f"rationale: {_fm_safe(proposal.rationale)}\n"
        f"proposed_at: {now.isoformat(timespec='seconds')}\n"
        f"---\n\n"
        f"{proposal.rule_body.rstrip()}\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def _parse_file(path: Path) -> Proposal:
    # re-sweep: mirror the JSON readers' corrupt-byte resilience
    # (team_memory uses errors="replace"). A proposal hand-edited in a
    # non-UTF-8 editor would otherwise raise UnicodeDecodeError and crash
    # the whole modulatio-standards review surface (list/show/approve/reject)
    # instead of degrading the one bad file. Strict decoding gives no upside
    # here — the parser tolerates arbitrary content already.
    raw = path.read_text(encoding="utf-8", errors="replace")
    m = _FRONTMATTER_RE.match(raw)
    meta: dict[str, str] = {}
    body = raw
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        body = raw[m.end():].lstrip()
    refs_raw = meta.get("evidence_refs", "")
    refs = tuple(p.strip() for p in refs_raw.split(",") if p.strip())
    return Proposal(
        domain=meta.get("domain", ""),
        title=meta.get("title", ""),
        rule_body=body.rstrip(),
        evidence_refs=refs,
        rationale=meta.get("rationale", ""),
    )


def list_proposals(project_code: str) -> list[Proposal]:
    """Return every pending proposal. Empty list when no proposals dir
    (new project or all resolved)."""
    root = _proposals_dir(project_code)
    if not root.exists():
        return []
    return [_parse_file(p) for p in sorted(root.glob("*.md"))]


def list_ids(project_code: str) -> list[str]:
    """Return the ids (filename stems) of pending proposals in sorted
    order. Used by the CLI to enumerate options."""
    root = _proposals_dir(project_code)
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*.md"))


def load(proposal_id: str, project_code: str) -> Proposal:
    """Load one proposal by id (filename stem). Raises
    FileNotFoundError when absent."""
    path = _proposals_dir(project_code) / f"{proposal_id}.md"
    if not path.exists():
        raise FileNotFoundError(
            f"proposal {proposal_id!r} not found under {path.parent}"
        )
    return _parse_file(path)


def _append_to_team_section(domain_file: Path, title: str, body: str) -> None:
    """Append ``### {title}\\n{body}`` under the single
    ``## Team-approved rules`` header in the domain standards file.
    Creates the header if it doesn't exist yet; never duplicates it.
    """
    entry = f"\n### {title}\n\n{body.rstrip()}\n"
    if not domain_file.exists():
        domain_file.parent.mkdir(parents=True, exist_ok=True)
        domain_file.write_text(f"{_TEAM_HEADER}\n{entry}", encoding="utf-8")
        return
    existing = domain_file.read_text(encoding="utf-8")
    if _TEAM_HEADER in existing:
        # Append entry at the end — header already present.
        with domain_file.open("a", encoding="utf-8") as f:
            f.write(entry)
    else:
        with domain_file.open("a", encoding="utf-8") as f:
            f.write(f"\n{_TEAM_HEADER}\n{entry}")


def approve(proposal_id: str, project_code: str) -> Path:
    """Approve a proposal: append its rule body to the project-local
    standards file for its domain, then delete the proposal file.
    Returns the path of the standards file that received the rule.
    """
    proposal = load(proposal_id, project_code)
    # Engine-bound invariant: the domain becomes a standards FILE NAME, so it
    # must be a safe single component — never a path-traversal that escapes the
    # standards/ root. validate_registry_name raises on anything with a
    # separator / '..' / leading dot, so a malicious or injected domain is
    # refused (fail-closed) rather than steering an arbitrary write.
    safe_domain = validate_registry_name(proposal.domain)
    domain_file = _standards_dir(project_code) / f"{safe_domain}.md"
    _append_to_team_section(domain_file, proposal.title, proposal.rule_body)
    # One-shot disposition — remove the proposal file.
    (_proposals_dir(project_code) / f"{proposal_id}.md").unlink()
    return domain_file


def reject(proposal_id: str, project_code: str) -> None:
    """Reject a proposal: delete the proposal file without touching
    project standards."""
    path = _proposals_dir(project_code) / f"{proposal_id}.md"
    if not path.exists():
        raise FileNotFoundError(
            f"proposal {proposal_id!r} not found under {path.parent}"
        )
    path.unlink()


__all__ = [
    "Proposal",
    "approve",
    "list_ids",
    "list_proposals",
    "load",
    "reject",
    "save",
]
