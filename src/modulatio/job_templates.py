# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Job Template registry — the Leader's self-authored job-SETUP memory.

A **Job Template (JT)** is the setup-side companion to a skill. Where a skill
captures *how to do a unit of work*, a JT captures *how to set up a class of
job*: the questions the Leader should ask the operator before planning, the
parameters those answers fill, and the shape of the output. It is domain-
AGNOSTIC — the engine reads nothing about "anthologies" or "briefs"; a JT is
pure operator-authored structure. The same format expresses a single report
(``cardinality: one``), N separate pieces over a list param (``per-item``), or
a literal count (``fixed:N``).

A JT has two states:
  - **unbound template** — this file: schema + question prose + defaults +
    output spec. Used to interview an operator (or run from defaults).
  - **bound instance** — the template plus a concrete answer set (a small JSON
    blob), runnable headless. That lives as state (a cron job's payload / a
    kickoff-history record), NOT in this library.

Files live in three locations, searched in order (project > shared > seed),
exactly like skills — a project-local JT fully replaces the shared one.

This module is the artifact format + loaders ONLY. No call sites;
the library/index, the interview, cron-binding, and self-codification land in
later bricks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modulatio import config
from modulatio.vault import project_dir, validate_registry_name

_JT_ROOT = config.get_shared_resources_path() / "job_templates"


def _fm(value: object) -> str:
    """Collapse CR/LF in a front-matter SCALAR value to a space so a value
    can't forge an extra front-matter line. The parser is
    line-based; a newline in a Leader-supplied ``description`` would otherwise
    inject a forged ``version`` / ``last_verified_at`` key."""
    return str(value).replace("\r", " ").replace("\n", " ")

#: Package-bundled seed JTs (read-only canonical defaults — e.g. the Leader's
#: ``jt-create`` drafting template). Resolved last, after the
#: user's shared + project-local copies, mirroring the skill seed dir.
_SEED_JT_ROOT = Path(__file__).parent / "_seed_job_templates"

_OWN_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class ParamField:
    """One operator-supplied parameter in a JT's schema.

    ``prompt`` is the conversational/machine duality: an LLM (or human)
    operator reads ``prompt`` and answers; a code operator fills ``name``
    directly from a bound-params dict. Same field, two consumers."""
    name: str
    type: str = "str"          # str | int | list[str] | enum | bool ... (advisory)
    required: bool = False
    default: Any = None
    prompt: str = ""           # the question to ask an interactive operator
    enum: tuple[str, ...] = ()  # allowed values when type == "enum"


@dataclass(frozen=True)
class OutputSpec:
    """The shape of a JT's output — the ONLY thing the engine branches on
    (purely by cardinality; it never knows the domain)."""
    cardinality: str = "one"        # "one" | "per-item" | "fixed:N"
    per: str | None = None          # the param field driving per-item fan-out
    artifact_kind: str = "document"  # document | code | ...
    naming: str = ""                 # naming template, e.g. "{competitor} — Brief"


@dataclass(frozen=True)
class DeliverableSpec:
    """Declared, CHECKABLE facts about the bound product (#101 Part C / C.0) — DATA the
    engine stamps + checks, never branched on. A SIBLING of OutputSpec (which is
    control-flow / cardinality only): OutputSpec is what the engine *branches* on and
    stays frozen; this is what the engine *checks against* and is free to grow. Every
    field optional; an empty spec == today's behavior. Product-agnostic: ``size_unit``
    is an OPTIONAL assertion of which measure the floor is in; left unset (default), the
    floor is judged in whatever unit the deliverable's own family counts (no privileged
    unit). Set it only to assert a specific family measure (e.g. ``rows`` for data)."""
    part_floor: int | None = None              # per-unit minimum, in ``size_unit``
    part_ceiling: int | None = None            # per-unit maximum (the QC band's top)
    size_unit: str = ""                        # "" = the family's native unit; else assert
    required_structure: tuple[str, ...] = ()   # structural elements, e.g. ("title", "toc")
    title: str | None = None                   # declared title (Part A's framing input)

    def is_empty(self) -> bool:
        """True when nothing checkable was declared — the engine then behaves exactly
        as it did before #101 (no floor judged, no structure required)."""
        return (
            self.part_floor is None
            and self.part_ceiling is None
            and not self.required_structure
            and self.title is None
        )


@dataclass(frozen=True)
class JobTemplate:
    """A loaded Job Template — setup schema + interview prose + output shape.

    Team-AGNOSTIC: ``capability_preferences`` expresses *preferences* only
    (free tags), never a pinned model — the engine always runs best-available
    with whatever Team exists (the never-block routing contract). ``version``
    is the self-codification marker: ``None`` for hand/seed JTs,
    ``"1"`` for the first autonomously-codified one, bumped on improvement; the
    full history is kept by git (the JT library is a git repo)."""

    name: str
    description: str
    interview_body: str
    output_spec: OutputSpec = field(default_factory=OutputSpec)
    deliverable_spec: DeliverableSpec = field(default_factory=DeliverableSpec)
    param_schema: tuple[ParamField, ...] = ()
    capability_preferences: tuple[str, ...] = ()
    version: str | None = None
    last_verified_at: str | None = None
    sources: tuple[str, ...] = ()

    # ── pure schema semantics (no I/O) — used by the interview / cron bind ──
    def defaults(self) -> dict[str, Any]:
        """The "just do it like I always do it" binding: every param at its
        declared default (params with no default are omitted)."""
        return {p.name: p.default for p in self.param_schema if p.default is not None}

    def missing_required(self, params: dict[str, Any]) -> list[str]:
        """Names of required params absent (or None) in ``params`` — the
        validation a cron-bind must pass before a headless run."""
        return [
            p.name for p in self.param_schema
            if p.required and params.get(p.name) is None
        ]

    def unfilled_required(self, params: dict[str, Any]) -> list[str]:
        """STRICT required check for the fit-gate: names of required params that
        are absent, None, an empty/whitespace-only string, or — for a list/tuple value
        — empty. Unlike :meth:`missing_required` (absent/None only, kept for cron.add's
        existing semantics), this catches the present-but-empty bypass (``{"topic": ""}``
        / ``{"competitors": []}``) so an explicit cron/operator bind that literally can't
        run is refused, not bound. Total over ``params``:
        a non-mapping is treated as "nothing supplied" → every required field is
        unfilled, never an ``AttributeError``."""
        if not isinstance(params, dict):
            params = {}
        unfilled: list[str] = []
        for p in self.param_schema:
            if not p.required:
                continue
            value = params.get(p.name)
            if value is None:
                unfilled.append(p.name)
            elif isinstance(value, str) and not value.strip():
                unfilled.append(p.name)
            elif isinstance(value, (list, tuple)) and len(value) == 0:
                unfilled.append(p.name)
        return unfilled

    def enum_violations(self, params: dict[str, Any]) -> list[str]:
        """Names of SUPPLIED params whose value falls outside a declared
        non-empty ``enum`` — a present-but-out-of-contract misfit a cron would mis-run
        every cycle. For a list/tuple value (a per-driver or list-typed field), any
        non-member item flags the field. Absence and emptiness are the presence check's
        job (:meth:`unfilled_required`), not flagged here; a field with no ``enum`` is
        unconstrained. Total over ``params`` (belt): a non-mapping → no supplied
        values → no enum violations (the malformed bind is caught by the gate)."""
        if not isinstance(params, dict):
            return []
        violations: list[str] = []
        for p in self.param_schema:
            if not p.enum:
                continue
            value = params.get(p.name)
            if value is None or (isinstance(value, str) and not value.strip()):
                continue  # absence/emptiness belongs to unfilled_required
            items = value if isinstance(value, (list, tuple)) else [value]
            if any(item not in p.enum for item in items):
                violations.append(p.name)
        return violations


_EMPTY_JT = JobTemplate(name="", description="", interview_body="")


def _parse_csv(raw: str) -> tuple[str, ...]:
    """``"a, b, c"`` (or ``"[a, b, c]"``) → tuple. Mirrors skills._parse_csv."""
    s = raw.strip()
    if not s:
        return ()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return tuple(p.strip() for p in s.split(",") if p.strip())


def _parse_param_schema(raw: str) -> tuple[ParamField, ...]:
    """Parse the single-line JSON ``param_schema`` value into ParamFields.
    Best-effort: any malformed JSON → empty schema (never raises)."""
    raw = raw.strip()
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return ()
    if not isinstance(data, dict):
        return ()
    fields: list[ParamField] = []
    for fname, spec in data.items():  # dict order = declaration order (py3.7+)
        if not isinstance(spec, dict):
            continue
        enum_raw = spec.get("enum")
        fields.append(ParamField(
            name=str(fname),
            type=str(spec.get("type", "str")),
            required=bool(spec.get("required", False)),
            default=spec.get("default"),
            prompt=str(spec.get("prompt", "")),
            enum=tuple(str(e) for e in enum_raw) if isinstance(enum_raw, list) else (),
        ))
    return tuple(fields)


def _normalize_cardinality(raw: str, per: str | None) -> str:
    """Coerce a stored ``cardinality`` string to the engine's grammar —
    ``one`` | ``fixed:N`` (N>=1) | ``per-item`` — mirroring the create-time
    validation (orchestration ``create_job_template``). Case-insensitive and
    space-tolerant (``Fixed: 8`` → ``fixed:8``; a bare ``8`` → ``fixed:8``).
    Anything unrecognized — including ``fixed:0``/``fixed:-1`` and a ``per-item``
    with no ``per`` to fan over — degrades to ``one`` (the safe default), so a
    hand-edited or migrated template can't smuggle a literal the engine branches
    on into the run; the only thing the engine fans out on stays well-formed."""
    norm = (raw or "").strip()
    low = norm.lower()
    if norm.isdigit():
        card = f"fixed:{norm}"
    elif low.startswith("fixed:"):
        card = "fixed:" + norm.split(":", 1)[1].strip()
    elif low.startswith("per-item"):
        card = "per-item"
    elif low == "one":
        card = "one"
    else:
        return "one"
    if card.startswith("fixed:"):
        n = card.split(":", 1)[1]
        return card if (n.isdigit() and int(n) >= 1) else "one"
    if card == "per-item":
        return "per-item" if per else "one"
    return card


def _parse_output_spec(raw: str) -> OutputSpec:
    """Parse the single-line JSON ``output_spec`` value. Best-effort → default
    (``cardinality: one``) on anything malformed — including a cardinality that
    doesn't match the engine's grammar (it normalizes the same way the create
    tool validates, so the load path can't bypass create-time validation)."""
    raw = raw.strip()
    if not raw:
        return OutputSpec()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return OutputSpec()
    if not isinstance(data, dict):
        return OutputSpec()
    per = str(data["per"]) if data.get("per") else None
    return OutputSpec(
        cardinality=_normalize_cardinality(str(data.get("cardinality", "one")), per),
        per=per,
        artifact_kind=str(data.get("artifact_kind", "document")),
        naming=str(data.get("naming", "")),
    )


def _parse_deliverable_spec(raw: str) -> DeliverableSpec:
    """Parse the single-line JSON ``deliverable_spec`` value. Best-effort → an EMPTY
    spec (today's behavior) on anything absent or malformed — never raises, never
    invents a constraint."""
    raw = raw.strip()
    if not raw:
        return DeliverableSpec()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return DeliverableSpec()
    if not isinstance(data, dict):
        return DeliverableSpec()

    def _int(v: Any) -> int | None:
        # A JSON bool is a Python int subclass — `part_floor: true` must NOT
        # silently become a floor of 1 (and `false` a 0). A non-integral float
        # (`2.9`) must not silently truncate to 2 either: these are author
        # mistakes, not constraints — drop them like any other malformed value.
        if isinstance(v, bool) or v is None:
            return None
        if isinstance(v, float):
            return int(v) if v.is_integer() else None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    rs = data.get("required_structure")
    structure = tuple(str(x) for x in rs) if isinstance(rs, (list, tuple)) else ()
    return DeliverableSpec(
        part_floor=_int(data.get("part_floor")),
        part_ceiling=_int(data.get("part_ceiling")),
        size_unit=str(data.get("size_unit") or ""),
        required_structure=structure,
        title=str(data["title"]) if data.get("title") else None,
    )


def _dump_param_schema(fields: tuple[ParamField, ...]) -> str:
    """ParamFields → single-line JSON (round-trips through _parse_param_schema).
    Omits falsy/default attributes to keep the line lean + git-diffable."""
    obj: dict[str, dict[str, Any]] = {}
    for f in fields:
        d: dict[str, Any] = {"type": f.type}
        if f.required:
            d["required"] = True
        if f.default is not None:
            d["default"] = f.default
        if f.prompt:
            d["prompt"] = f.prompt
        if f.enum:
            d["enum"] = list(f.enum)
        obj[f.name] = d
    return json.dumps(obj, ensure_ascii=False)


def _dump_output_spec(spec: OutputSpec) -> str:
    obj: dict[str, Any] = {"cardinality": spec.cardinality}
    if spec.per:
        obj["per"] = spec.per
    obj["artifact_kind"] = spec.artifact_kind
    if spec.naming:
        obj["naming"] = spec.naming
    return json.dumps(obj, ensure_ascii=False)


def _dump_deliverable_spec(spec: DeliverableSpec) -> str:
    """DeliverableSpec → single-line JSON (round-trips through _parse_deliverable_spec).
    Omits unset fields to keep the frontmatter lean + git-diffable; ``size_unit`` rides
    only when a floor/ceiling makes it meaningful."""
    obj: dict[str, Any] = {}
    if spec.part_floor is not None:
        obj["part_floor"] = spec.part_floor
    if spec.part_ceiling is not None:
        obj["part_ceiling"] = spec.part_ceiling
    if spec.size_unit and (spec.part_floor is not None or spec.part_ceiling is not None):
        obj["size_unit"] = spec.size_unit
    if spec.required_structure:
        obj["required_structure"] = list(spec.required_structure)
    if spec.title:
        obj["title"] = spec.title
    return json.dumps(obj, ensure_ascii=False)


def _parse_file(path: Path) -> JobTemplate:
    raw = path.read_text(encoding="utf-8", errors="replace")
    m = _OWN_FRONTMATTER_RE.match(raw)
    meta: dict[str, str] = {}
    body = raw
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")  # first colon only — JSON values keep theirs
                meta[k.strip()] = v.strip()
        body = raw[m.end():].lstrip()
    else:
        body = raw.lstrip()

    return JobTemplate(
        name=meta.get("name") or path.stem,
        description=meta.get("description") or "",
        interview_body=body,
        output_spec=_parse_output_spec(meta.get("output_spec", "")),
        deliverable_spec=_parse_deliverable_spec(meta.get("deliverable_spec", "")),
        param_schema=_parse_param_schema(meta.get("param_schema", "")),
        capability_preferences=_parse_csv(meta.get("capability_preferences", "")),
        version=meta.get("version") or None,
        last_verified_at=meta.get("last_verified_at") or None,
        sources=(str(path),),
    )


def load_with_metadata(name: str, project_code: str | None = None) -> JobTemplate:
    """Load a JT by name. Resolution chain (highest precedence first):

    1. Project-local at ``<project>/job_templates/<name>.md``
    2. User-curated shared at ``<shared_resources>/job_templates/<name>.md``
    3. Package-bundled seed at ``modulatio/_seed_job_templates/<name>.md``

    Missing in all three → empty JT (``.name == ""``), not an error.
    """
    # A traversal name resolves to the empty JT (safe not-found) rather
    # than a read outside the registry root.
    try:
        validate_registry_name(name)
    except ValueError:
        return _EMPTY_JT
    if project_code is not None:
        local = project_dir(project_code) / "job_templates" / f"{name}.md"
        if local.exists():
            return _parse_file(local)
    shared = _JT_ROOT / f"{name}.md"
    if shared.exists():
        return _parse_file(shared)
    seed = _SEED_JT_ROOT / f"{name}.md"
    if seed.exists():
        return _parse_file(seed)
    return _EMPTY_JT


def load_interview(name: str, project_code: str | None = None) -> str:
    """Return the JT's interview-body prose (empty string if missing)."""
    return load_with_metadata(name, project_code).interview_body


def list_job_templates(project_code: str | None = None) -> list[str]:
    """Sorted union of JT names visible to a project (seed + shared + local)."""
    names: set[str] = set()
    if _SEED_JT_ROOT.exists():
        names.update(p.stem for p in _SEED_JT_ROOT.glob("*.md"))
    if _JT_ROOT.exists():
        names.update(p.stem for p in _JT_ROOT.glob("*.md"))
    if project_code is not None:
        local_dir = project_dir(project_code) / "job_templates"
        if local_dir.exists():
            names.update(p.stem for p in local_dir.glob("*.md"))
    return sorted(names)


def save(jt: JobTemplate, project_code: str | None = None) -> Path:
    """Write a JT to shared (no project_code) or the project's override dir.
    Nested schema/output written as single-line JSON. Returns the written path."""
    validate_registry_name(jt.name)  # no path-traversal write
    if project_code is not None:
        root = project_dir(project_code) / "job_templates"
    else:
        root = _JT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{jt.name}.md"

    # Scalar values are newline-collapsed (``_fm``) so a Leader-supplied
    # field can't forge a front-matter key. The nested specs are single-line
    # JSON (newlines already escaped), so they're injection-safe as-is.
    fm_lines: list[str] = [
        f"name: {jt.name}",
        f"description: {_fm(jt.description)}",
        f"capability_preferences: {_fm(', '.join(jt.capability_preferences))}",
        f"param_schema: {_dump_param_schema(jt.param_schema)}",
        f"output_spec: {_dump_output_spec(jt.output_spec)}",
    ]
    if not jt.deliverable_spec.is_empty():
        fm_lines.append(f"deliverable_spec: {_dump_deliverable_spec(jt.deliverable_spec)}")
    if jt.version is not None:
        fm_lines.append(f"version: {_fm(jt.version)}")
    if jt.last_verified_at is not None:
        fm_lines.append(f"last_verified_at: {_fm(jt.last_verified_at)}")

    content = "---\n" + "\n".join(fm_lines) + "\n---\n\n" + jt.interview_body.rstrip() + "\n"
    path.write_text(content, encoding="utf-8")
    return path


def create_job_template(
    *,
    name: str,
    description: str,
    interview_body: str,
    output_spec: OutputSpec | None = None,
    deliverable_spec: "DeliverableSpec | None" = None,
    param_schema: tuple[ParamField, ...] = (),
    capability_preferences: tuple[str, ...] = (),
    version: str | None = None,
    project_code: str | None = None,
) -> JobTemplate:
    """Create a new JT file, shared or project-local.

    Idempotency: raises ``FileExistsError`` if a JT with the same name exists
    at the target scope (the name-dedup hard guard; codification
    flips create→improve on collision rather than colliding here)."""
    validate_registry_name(name)  # no path-traversal write
    if project_code is not None:
        root = project_dir(project_code) / "job_templates"
    else:
        root = _JT_ROOT
    target = root / f"{name}.md"
    if target.exists():
        raise FileExistsError(
            f"job template already exists at {target}; pick a different name "
            f"or improve the existing one"
        )

    jt = JobTemplate(
        name=name,
        description=description,
        interview_body=interview_body,
        output_spec=output_spec or OutputSpec(),
        deliverable_spec=deliverable_spec or DeliverableSpec(),
        param_schema=param_schema,
        capability_preferences=capability_preferences,
        version=version,
    )
    save(jt, project_code)
    return jt


__all__ = [
    "JobTemplate",
    "OutputSpec",
    "DeliverableSpec",
    "ParamField",
    "create_job_template",
    "list_job_templates",
    "load_interview",
    "load_with_metadata",
    "save",
]
