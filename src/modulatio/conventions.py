"""Engine-derived convention contracts (plan-time component authority).

A multi-file code plan needs ONE settled answer to "what is this component
called and where do its files live" BEFORE any producer runs — otherwise
each producer invents its own package name/layout and the modules never
cohere. This module derives that answer deterministically from the plan
itself, in strict evidence order:

1. explicit task output targets (the plan's own declared paths),
2. manifests already present in the exact component tree,
3. ecosystem-specific normalization.

Inspection is scoped to the tasks' component — an unrelated existing
package elsewhere in the workspace can never win discovery. Ambiguity
(mixed layouts, loose scripts beside a package, multiple plausible roots
for one target) is a typed ``unresolved`` result, never a guessed
immutable choice; a genuinely standalone script is a RESOLVED standalone
contract that invents no package.

v1 closure claim: the ``python`` ecosystem plus the neutral standalone
form. Any other code ecosystem derives ``unresolved``.
"""

from __future__ import annotations

import hashlib
import json
import keyword
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from modulatio.types import ConventionContract, ConventionContractConflict, Task

__all__ = [
    "ConventionContract",
    "ConventionContractConflict",
    "DerivationResult",
    "UnresolvedConvention",
    "contract_digest",
    "derive_convention_contracts",
    "is_code_artifact_kind",
    "plan_digest",
    "render_contract_block",
    "target_root_violation",
    "task_plan_projection_digest",
    "validate_sealed_contract",
]


#: The IMMUTABLE plan shape of a task — what planning decided, none of what
#: execution mutates. The prepared manifest digests exactly these fields,
#: so a dispatched/retried/reassigned task still validates while an altered
#: target, dependency, or contract binding never does.
_PLAN_PROJECTION_FIELDS = (
    "id", "goal_id", "description", "artifact_kind", "operation",
    "output_path", "depends_on", "required_skills",
    "required_capabilities", "research_topics", "tool_args",
    "deliverable", "convention_contract_id", "max_retries",
)


def task_plan_projection_digest(task: Task) -> str:
    """Digest of the task's immutable plan projection (the prepared
    manifest's per-task witness)."""
    payload = {f: getattr(task, f) for f in _PLAN_PROJECTION_FIELDS}
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_digest(
    expected_task_ids: list[str], expected_task_digests: dict[str, str],
) -> str:
    """Whole-plan digest: ordered membership + every projection. Stamped
    at commit; any drift in order, set, or projections breaks it."""
    canonical = json.dumps(
        {"ids": list(expected_task_ids),
         "digests": dict(sorted(expected_task_digests.items()))},
        sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


CODE_KIND_TOKENS = frozenset({
    "code", "python", "py", "module", "script", "test", "tests",
    "javascript", "js", "typescript", "ts",
})


def is_code_artifact_kind(kind: str | None) -> bool:
    """True iff ``kind`` names a code artifact (token match, so composite
    kinds like ``python-module`` qualify)."""
    if not kind:
        return False
    parts = re.split(r"[^a-zA-Z0-9]+", kind.lower())
    return any(p in CODE_KIND_TOKENS for p in parts)


#: Non-``.py`` filenames a Python component may legitimately declare as
#: task targets (manifest/config/docs) without tripping the unsupported-
#: ecosystem disposition.
_COMPONENT_NEUTRAL_FILES = frozenset({
    "pyproject.toml", "setup.py", "setup.cfg", "readme.md", ".gitignore",
    "makefile", "license", "license.md", "license.txt",
})


@dataclass(frozen=True)
class UnresolvedConvention:
    """One component (or target group) that could not settle to a single
    convention authority, with the mechanism reason and the task ids it
    strands. A coherence-requiring plan carrying one of these must reject
    before dispatch — labeled ambiguity is not prevention."""

    reason: str
    task_ids: tuple[str, ...]


@dataclass(frozen=True)
class DerivationResult:
    contracts: list[ConventionContract] = field(default_factory=list)
    #: task id → contract id, for every code task of a resolved component.
    bindings: dict[str, str] = field(default_factory=dict)
    #: Components that NEED a settled convention but cannot get one
    #: (naming failure, mixed layouts, Python ambiguity) — a plan carrying
    #: any of these rejects before dispatch.
    unresolved: list[UnresolvedConvention] = field(default_factory=list)
    #: Targets EXPLICITLY outside the v1 closure claim (non-Python
    #: ecosystems, undeclared drafts). They run unbound — and unbound
    #: means no surface can present them as convention-enforced. Distinct
    #: from ``unresolved``: no coherence claim exists to settle.
    outside_claim: list[UnresolvedConvention] = field(default_factory=list)


def contract_digest(contract: ConventionContract) -> str:
    """Content digest over every convention field (identity and digest
    excluded) — the write-once witness and the recovery comparator."""
    payload = contract.model_dump(exclude={"contract_id", "digest"})
    canonical = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


#: Sealed schema versions this engine can serve as authority.
_SUPPORTED_SCHEMA_VERSIONS = frozenset({1})


def validate_sealed_contract(contract: ConventionContract) -> str | None:
    """Mechanism reason ``contract`` is not servable sealed authority, or
    None when it is.

    Content, identity, and schema are checked together. A digest that
    merely agrees with its own content proves nothing — anything editing a
    record in place can recompute it. Identity is derived FROM the digest,
    so only the pair binds the record to the derivation that sealed it: a
    changed convention necessarily changes the digest, and a retained
    identity then no longer matches.
    """
    if contract.schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
        return (f"contract {contract.contract_id} declares schema version "
                f"{contract.schema_version}, which this engine cannot serve")
    if contract.state != "resolved":
        return (f"contract {contract.contract_id} is {contract.state!r} — "
                f"only a resolved contract is authority")
    digest = contract_digest(contract)
    if digest != contract.digest:
        return (f"contract {contract.contract_id} fails its own digest — "
                f"the sealed record was altered and is not authority")
    if contract.contract_id != f"cvc-{digest[:12]}":
        return (f"contract {contract.contract_id} does not derive its "
                f"identity from its content — the record carries an "
                f"identity its conventions did not seal")
    return None


def _sealed(**fields) -> ConventionContract:
    """Construct a contract with its digest-derived identity."""
    draft = ConventionContract(contract_id="", digest="", **fields)
    digest = contract_digest(draft)
    return draft.model_copy(
        update={"digest": digest, "contract_id": f"cvc-{digest[:12]}"})


def _valid_import_name(name: str) -> str | None:
    """Mechanism reason the name cannot be a Python import identifier, or
    None when it can. Distribution naming is validated separately."""
    if not name.isidentifier():
        return f"{name!r} is not a valid Python import identifier"
    if keyword.iskeyword(name):
        return f"{name!r} is a Python reserved word"
    if name in sys.stdlib_module_names:
        return f"{name!r} collides with a standard-library module"
    return None


def _distribution_name(
    inspect_root: Path, component_root: str, import_name: str,
) -> tuple[str, str]:
    """(distribution name, manifest filename). Evidence order: a manifest
    in the exact component tree names the distribution; otherwise the
    import name normalizes. Never scans outside the component."""
    manifest = (
        inspect_root / component_root / "pyproject.toml"
        if component_root else inspect_root / "pyproject.toml"
    )
    try:
        if manifest.is_file():
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
            name = data.get("project", {}).get("name")
            if isinstance(name, str) and name.strip():
                return name.strip(), "pyproject.toml"
    except (OSError, tomllib.TOMLDecodeError):
        # An unreadable manifest is absent evidence, not a verdict —
        # normalization below still yields a deterministic name.
        pass
    return import_name, "pyproject.toml"


def _component_of(
    path: PurePosixPath, inspect_root: Path,
) -> "tuple[str, str, str] | None":
    """``(component_root, import_name, layout)`` for a declared target, or
    None when more than one component boundary plausibly explains it.

    The boundary comes from the declared path SHAPE, never from how many
    tasks happen to share it. A ``src`` directory marks the boundary
    explicitly: everything before it is the component, the directory after
    it is the package. A flat path has no such marker, so a nesting level
    is only a component boundary when a manifest sits there; with no
    manifest anywhere the path is one package with subdirectories, and
    with several the choice is not the engine's to guess.
    """
    dirs = list(path.parts[:-1])
    src_at = [i for i, part in enumerate(dirs) if part == "src"]
    if len(src_at) > 1:
        return None
    if src_at:
        i = src_at[0]
        if i == len(dirs) - 1:
            # ``src`` declares a src layout but names no package under it.
            return None
        return "/".join(dirs[:i]), dirs[i + 1], "src"
    if len(dirs) == 1:
        return "", dirs[0], "flat"
    manifested = [
        j for j in range(1, len(dirs))
        if (inspect_root / "/".join(dirs[:j]) / "pyproject.toml").is_file()
    ]
    if not manifested:
        return "", dirs[0], "flat"
    if len(manifested) == 1:
        j = manifested[0]
        return "/".join(dirs[:j]), dirs[j], "flat"
    return None


def _standalone_contract(path: PurePosixPath | None) -> ConventionContract:
    stem = path.stem if path is not None else ""
    import_name = stem if (stem and _valid_import_name(stem) is None) else ""
    return _sealed(
        ecosystem="python", state="resolved", layout="standalone",
        component_root="", source_root="", test_root="",
        import_name=import_name, distribution_name="", manifest_filename="")


def derive_convention_contracts(
    tasks: list[Task], *, component_inspect_root: Path,
) -> DerivationResult:
    """Derive one sealed contract per component from the plan's code tasks.

    Pure over the task list except for one scoped read: the component
    tree's own manifest. Deterministic — identical plans yield identical
    digests and bindings.
    """
    code_tasks = [t for t in tasks if is_code_artifact_kind(t.artifact_kind)]
    if not code_tasks:
        return DerivationResult()

    targets: list[tuple[Task, PurePosixPath | None]] = []
    for t in code_tasks:
        if t.output_path:
            targets.append((t, PurePosixPath(t.output_path.lstrip("./"))))
        else:
            targets.append((t, None))

    # A task that declares NO target has no path shape to classify, and a
    # lone undeclared code task is the standalone form. Every DECLARED
    # path is classified by its shape below, whatever the task count — a
    # package-shaped target is a package even when only one task writes to
    # it.
    if len(targets) == 1 and targets[0][1] is None:
        task = targets[0][0]
        contract = _standalone_contract(None)
        return DerivationResult(
            contracts=[contract], bindings={task.id: contract.contract_id})

    # Group targets into components by the boundary their declared paths
    # imply. Root-level test targets attach to the single component; loose
    # root PYTHON scripts beside a package are ambiguity, not membership;
    # non-Python targets are outside the claim, never poison it.
    outside_claim: list[UnresolvedConvention] = []
    pathless = [t for t, p in targets if p is None]
    if pathless:
        # Undeclared targets (drafts fallback) declare no structure to
        # cohere — no claim exists, none is invented.
        outside_claim.append(UnresolvedConvention(
            reason=("pathless code task(s) — no declared structure, no "
                    "convention claim"),
            task_ids=tuple(t.id for t in pathless)))

    root_tests: list[tuple[Task, PurePosixPath]] = []
    loose_py: list[tuple[Task, PurePosixPath]] = []
    foreign_loose: list[tuple[Task, PurePosixPath]] = []
    ambiguous: list[tuple[Task, PurePosixPath]] = []
    #: (component root, import name) → the targets that component owns.
    groups: dict[tuple[str, str], list[tuple[Task, PurePosixPath]]] = {}
    layouts: dict[tuple[str, str], set[str]] = {}
    for task, path in targets:
        if path is None:
            continue
        parts = path.parts
        if len(parts) == 1:
            if path.suffix == ".py":
                loose_py.append((task, path))
            elif path.name.lower() in _COMPONENT_NEUTRAL_FILES:
                pass  # root manifest/docs carry no component membership
            else:
                foreign_loose.append((task, path))
            continue
        if parts[0] == "tests":
            root_tests.append((task, path))
            continue
        if parts[-2] == "src":
            # ``src/<file>.py`` declares the src layout but names no
            # package under it, so the file belongs to no package — the
            # same standing as a root-level script, and never a component
            # called "src".
            if path.suffix == ".py":
                loose_py.append((task, path))
            elif path.name.lower() not in _COMPONENT_NEUTRAL_FILES:
                foreign_loose.append((task, path))
            continue
        component = _component_of(path, component_inspect_root)
        if component is None:
            ambiguous.append((task, path))
            continue
        component_root, name, layout = component
        groups.setdefault((component_root, name), []).append((task, path))
        layouts.setdefault((component_root, name), set()).add(layout)

    if foreign_loose:
        outside_claim.append(UnresolvedConvention(
            reason=("non-Python root-level target(s) — outside the v1 "
                    "closure claim"),
            task_ids=tuple(t.id for t, _ in foreign_loose)))

    unresolved: list[UnresolvedConvention] = []
    contracts: list[ConventionContract] = []
    bindings: dict[str, str] = {}

    if ambiguous:
        unresolved.append(UnresolvedConvention(
            reason=("more than one component boundary explains target(s) "
                    + ", ".join(sorted(str(p) for _, p in ambiguous))
                    + " — the component root cannot be chosen"),
            task_ids=tuple(sorted(t.id for t, _ in ambiguous))))

    if loose_py and groups:
        unresolved.append(UnresolvedConvention(
            reason=("loose root-level Python script(s) beside package "
                    "component(s) — no single component root explains "
                    "every target"),
            task_ids=tuple(t.id for t, _ in loose_py)))
    elif loose_py:
        # Independent standalone scripts: each its own resolved component.
        for task, path in loose_py:
            contract = _standalone_contract(path)
            contracts.append(contract)
            bindings[task.id] = contract.contract_id

    resolved_components: list[ConventionContract] = []
    for key in sorted(groups):
        component_root, name = key
        members = groups[key]
        member_ids = tuple(t.id for t, _ in members)
        py_members = [p for _, p in members if p.suffix == ".py"]
        foreign = [
            p for _, p in members
            if p.suffix != ".py"
            and p.name.lower() not in _COMPONENT_NEUTRAL_FILES
        ]
        if not py_members and foreign:
            # A wholly non-Python component: outside the claim — it runs,
            # it is simply never presented as convention-enforced.
            outside_claim.append(UnresolvedConvention(
                reason=(f"non-Python component {name!r} — outside the v1 "
                        f"closure claim"),
                task_ids=member_ids))
            continue
        if foreign:
            unresolved.append(UnresolvedConvention(
                reason=(f"component {name!r} mixes Python modules with "
                        f"{str(foreign[0])!r} — the Python claim is "
                        f"ambiguous"),
                task_ids=member_ids))
            continue
        if len(layouts[key]) > 1:
            unresolved.append(UnresolvedConvention(
                reason=(f"component {name!r} targeted through both src and "
                        f"flat layouts — one immutable layout cannot be "
                        f"chosen"),
                task_ids=member_ids))
            continue
        why_invalid = _valid_import_name(name)
        if why_invalid is not None:
            unresolved.append(UnresolvedConvention(
                reason=why_invalid, task_ids=member_ids))
            continue
        layout = layouts[key].pop()
        # Roots are stored RELATIVE to the component root, which is what
        # the import smoke and the target check both resolve against.
        source_root = f"src/{name}" if layout == "src" else name
        test_root = ""
        component_tests = [
            (t, p) for t, p in members
            if len(p.parts) >= 2 and p.parts[-2] == "tests"
        ]
        if component_tests:
            suite = component_tests[0][1].parent
            test_root = str(
                suite.relative_to(component_root) if component_root else suite)
        elif root_tests and not component_root:
            test_root = "tests"
        distribution, manifest = _distribution_name(
            component_inspect_root, component_root, name)
        contract = _sealed(
            ecosystem="python", state="resolved", layout=layout,
            component_root=component_root, source_root=source_root,
            test_root=test_root, import_name=name,
            distribution_name=distribution, manifest_filename=manifest)
        contracts.append(contract)
        resolved_components.append(contract)
        for tid in member_ids:
            bindings[tid] = contract.contract_id

    if root_tests:
        if len(resolved_components) == 1:
            target_id = resolved_components[0].contract_id
            for task, _ in root_tests:
                bindings[task.id] = target_id
        elif groups:
            unresolved.append(UnresolvedConvention(
                reason=("root-level tests cannot attach to a single "
                        f"resolved component ({len(groups)} candidates)"),
                task_ids=tuple(t.id for t, _ in root_tests)))
        else:
            outside_claim.append(UnresolvedConvention(
                reason=("root-level tests with no package component — no "
                        "convention claim to attach to"),
                task_ids=tuple(t.id for t, _ in root_tests)))

    return DerivationResult(
        contracts=contracts, bindings=bindings, unresolved=unresolved,
        outside_claim=outside_claim)


def target_root_violation(
    contract: ConventionContract, output_path: str,
) -> str | None:
    """Mechanism reason ``output_path`` lies outside the contract's
    declared roots, or None when compatible. Standalone contracts claim
    no roots; component-neutral files (manifest/docs) live at the
    component root legitimately."""
    if contract.state != "resolved" or contract.layout == "standalone":
        return None
    path = PurePosixPath(output_path.lstrip("./"))
    if path.name.lower() in _COMPONENT_NEUTRAL_FILES:
        return None
    # The contract's roots are relative to its component, so a declared
    # target is compared against the component-qualified roots.
    prefix = f"{contract.component_root}/" if contract.component_root else ""
    allowed = [f"{prefix}{contract.source_root}"]
    if contract.test_root:
        allowed.append(f"{prefix}{contract.test_root}")
    for root in allowed:
        if root and str(path).startswith(root + "/"):
            return None
    return (
        f"target {str(path)!r} lies outside the component's declared "
        f"roots ({', '.join(r for r in allowed if r)})"
    )


def render_contract_block(contract: ConventionContract) -> str:
    """The one rendered convention truth, injected identically into
    producer, QC, and fixer prompts. Fails closed on any non-renderable
    state — an unresolved contract must never present as enforced
    coherence."""
    if contract.state != "resolved":
        raise ConventionContractConflict(
            f"contract {contract.contract_id or '<unidentified>'} is "
            f"{contract.state!r} — only a resolved contract renders")
    lines = ["## Project conventions", ""]
    if contract.layout == "standalone":
        name = contract.import_name or "the declared file"
        lines += [
            f"- Deliverable: a single standalone Python file ({name}).",
            "- Do NOT invent a package, extra modules, or config files — "
            "the whole component is this one file.",
        ]
    else:
        lines += [
            f"- Ecosystem: Python · layout: {contract.layout}",
            f"- Import name: `{contract.import_name}` · distribution "
            f"name: `{contract.distribution_name}`",
            f"- Source root: `{contract.source_root}`"
            + (f" · test root: `{contract.test_root}`"
               if contract.test_root else ""),
            f"- Manifest: `{contract.manifest_filename}`"
            if contract.manifest_filename else "",
            f"Every module lives under `{contract.source_root}` and is "
            f"imported as `{contract.import_name}`. Use these exact names "
            f"— do not invent alternate package, module, or config names.",
        ]
    return "\n".join(line for line in lines if line != "")
