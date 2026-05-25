"""Project design-intent — the WHY behind THIS project's intentional choices.

Pre-V2 Slice B. Distinct from team_memory (curated cross-run wisdom) and
team_canvas (in-flight artifacts in this run). Design-intent is the
project-scoped, binding constraints + reasons that the team should
honor: "we use markdown not SQLite — human-readable artifact trail,"
"model-agnostic, never hardcode model names," "Python stdlib only — no
external dependencies for the MVP," etc.

Lives at ``<project>/standards/design-intent.md`` with frontmatter
``type: design-intent``. Read at producer / task-plan / QC / Leader
prompt-build time and injected as a ``{design_intent}`` slot.

Cross-references the design-intent ✕ wisdom complementary-axis design
captured 2026-04-30 (memory: project_modulatio_design_intent_grounding_gap.md).
The dual-layer model: design-intent BINDS this project; wisdom (cross-
project) ADVISES. Wisdom isn't yet built; design-intent is the first
half of the axis.

V1 scope (this slice):
- Read + parse the file (yaml frontmatter + markdown body)
- Inject the body into producer / task-plan / Leader / QC prompts
- 5th QC TQM axis ``design_intent_alignment`` exists as a slot that QC
  consults; no automatic enforcement primitive yet — QC's prompt asks
  it to flag obvious violations, but no structured-output schema
  rejects on this axis alone

scope (deferred): the QC-axis enforcement primitive. Wisdom layer.
Architect-class prompt-injection rules. Conflict-resolution flow when
wisdom contradicts design-intent.
"""

from __future__ import annotations

import re
from pathlib import Path

from modulatio.vault import project_dir, validate_project_code


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def design_intent_path(project_code: str) -> Path:
    """Canonical location of the project's design-intent file. Returns
    the path even when the file doesn't exist — callers check
    ``.exists()`` themselves."""
    code = validate_project_code(project_code.lower())
    return project_dir(code) / "standards" / "design-intent.md"


def load_body(project_code: str) -> str:
    """Read the design-intent markdown body (frontmatter stripped).

    Empty string when the file doesn't exist OR when the file is
    malformed (no frontmatter / can't parse). Defensive — a broken
    design-intent file should NOT block producer dispatch; it just
    means the team executes without intent context, same as a project
    that hasn't authored one.
    """
    path = design_intent_path(project_code)
    if not path.exists():
        return ""
    try:
        raw = path.read_text()
    except OSError:
        return ""
    m = _FRONTMATTER_RE.match(raw)
    if m is None:
        # No frontmatter → treat the whole file as body. Lenient
        # because some users will hand-author the file without
        # bothering with the type marker.
        return raw.strip()
    return raw[m.end():].strip()


def render_for_prompt(project_code: str) -> str:
    """Build the rendered ``{design_intent}`` slot for prompt
    injection. Returns the full block including section header +
    delimiters, or a neutral marker when the project has no
    design-intent file yet.

    Producer / task-plan / QC / Leader prompts all consume this
    same rendered string — single source of truth for the format,
    avoids drift across the four call sites.
    """
    body = load_body(project_code)
    if not body:
        return (
            "DESIGN INTENT — project-scoped binding constraints + "
            "intentional choices\n\n"
            "(No design-intent file authored for this project yet. "
            "The team operates without project-specific constraints.)"
        )
    return (
        "DESIGN INTENT — project-scoped binding constraints + "
        "intentional choices. Honor these unless explicitly authorized "
        "to deviate. The reasons matter: when in doubt, prefer the "
        "choice that aligns with the documented intent.\n\n"
        "-----BEGIN DESIGN INTENT-----\n"
        f"{body}\n"
        "-----END DESIGN INTENT-----"
    )


__all__ = [
    "design_intent_path",
    "load_body",
    "render_for_prompt",
]
