# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Agent template registry.

Templates are markdown files with frontmatter — same format as skills and
standards. Two locations, searched in order:

1. **Shared defaults** at ``<shared_resources>/templates/<template_id>.md``
   — user-installed overrides (placed by setup wizard, slice 3).
2. **Bundled defaults** in this package directory — the templates that
   ship with Modulatio. Loaded when the user hasn't installed a custom
   override.

Skills-first (#143):
- The **Leader** is the one required structural role — exactly one per install.
  **QC** (`qc`) is structural too (the verifier) but OPTIONAL: a solo-Leader
  setup can skip it. (The legacy `coordinator` triad template was removed —
  planning is the Leader's job.)
- Worker templates (`writer`, `coder`, `analyst`, `researcher`,
  `editor`, `marketer`, `qa-engineer`) are optional skill-holders.
- A team is 1–10 agents: the Leader alone, optionally a QC, plus any producers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from modulatio import config

_BUNDLED_TEMPLATES_DIR = Path(__file__).parent

_OWN_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class Template:
    """A loaded agent template — frontmatter + identity body.

    Used by the setup wizard to seed agents in the project roster.
    """

    template_id: str
    name: str
    tier: str  # leader / qc / producer  (coordinator = removed legacy)
    default_skills: tuple[str, ...]
    default_capability_tags: tuple[str, ...]
    default_model_tier: str
    cost_class: str
    mandatory: bool  # True = structural template (Leader/QC); False = worker.
                     # "structural", NOT "must be in every roster" — QC is
                     # skippable in setup (#13); only the Leader is required.
    description: str
    identity: str  # the body — used as Agent.identity
    source_path: Path | None = None


# Skills-first: the structural roles are Leader (the one REQUIRED deliberative
# seat) and QC (the verifier — structural but OPTIONAL since #13; a solo-Leader
# setup skips it). Planning is the Leader's job — a prior standalone
# "coordinator" role was removed engine-side. Everything else is a producer
# (skill-holder) the team routes work to.
_TRIAD_TIERS = ("leader", "qc")
_WORKER_TEMPLATES = ("writer", "coder", "analyst", "researcher", "editor", "marketer", "qa-engineer")
_TRIAD_TEMPLATES = ("leader", "qc")


def _shared_dir() -> Path:
    """User-installed override location for templates."""
    return config.get_shared_resources_path() / "templates"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Strip and parse YAML frontmatter; return (fields_dict, body_text)."""
    m = _OWN_FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fields: dict[str, object] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        raw = raw.strip()
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            if not inner:
                fields[key] = []
            else:
                fields[key] = [item.strip() for item in inner.split(",") if item.strip()]
        elif raw.lower() in ("true", "false"):
            fields[key] = raw.lower() == "true"
        else:
            fields[key] = raw
    body = text[m.end():]
    return fields, body


def _load_from_path(path: Path) -> Template | None:
    """Parse a template markdown file. Returns None if missing/malformed."""
    if not path.exists() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fields, body = _parse_frontmatter(text)
    return Template(
        template_id=str(fields.get("template_id", path.stem)),
        name=str(fields.get("name", path.stem.title())),
        tier=str(fields.get("tier", "producer")),
        default_skills=tuple(fields.get("default_skills", [])),
        default_capability_tags=tuple(fields.get("default_capability_tags", [])),
        default_model_tier=str(fields.get("default_model_tier", "generalist")),
        cost_class=str(fields.get("cost_class", "paid-cloud")),
        mandatory=bool(fields.get("mandatory", False)),
        description=str(fields.get("description", "")),
        identity=body.strip(),
        source_path=path,
    )


def get_template(template_id: str) -> Template | None:
    """Load a template by id. Project-shared overrides win over bundled defaults."""
    # 1. Check user-installed override
    override = _load_from_path(_shared_dir() / f"{template_id}.md")
    if override is not None:
        return override
    # 2. Fall back to bundled default
    return _load_from_path(_BUNDLED_TEMPLATES_DIR / f"{template_id}.md")


def list_templates(*, mandatory_only: bool = False, workers_only: bool = False) -> list[Template]:
    """Return all available templates (overrides + bundled defaults).

    User overrides supersede bundled entries with the same id.

    Args:
        mandatory_only: only return structural-role templates (Leader/QC)
        workers_only: only return worker templates (writer/coder/...)
    """
    seen: set[str] = set()
    out: list[Template] = []

    # Overrides first
    shared = _shared_dir()
    if shared.exists():
        for f in sorted(shared.glob("*.md")):
            t = _load_from_path(f)
            if t is None:
                continue
            seen.add(t.template_id)
            out.append(t)

    # Bundled defaults that aren't overridden
    for f in sorted(_BUNDLED_TEMPLATES_DIR.glob("*.md")):
        t = _load_from_path(f)
        if t is None or t.template_id in seen:
            continue
        seen.add(t.template_id)
        out.append(t)

    if mandatory_only:
        out = [t for t in out if t.mandatory]
    if workers_only:
        out = [t for t in out if not t.mandatory]
    return out


def list_template_ids(**kwargs) -> list[str]:
    """Convenience: just the ids."""
    return [t.template_id for t in list_templates(**kwargs)]


def triad_templates() -> dict[str, Template]:
    """Return {tier: template} for the structural slots (Leader, QC).

    Picks the first template per tier in load order. If multiple shared
    overrides exist for the same tier, the one earliest by template id wins.
    """
    by_tier: dict[str, Template] = {}
    for t in list_templates(mandatory_only=True):
        if t.tier in _TRIAD_TIERS and t.tier not in by_tier:
            by_tier[t.tier] = t
    return by_tier


def worker_templates() -> list[Template]:
    """Return all worker (non-triad) templates available to the wizard."""
    return list_templates(workers_only=True)


__all__ = [
    "Template",
    "get_template",
    "list_templates",
    "list_template_ids",
    "triad_templates",
    "worker_templates",
]
