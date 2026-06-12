"""Deterministic oracles for the ``code`` and ``media`` assembly families (#100).

``review_ledger.verify_assembly`` lets an assembly PASS QC *cheaply* — without
re-reading the assembled bytes back into the model — when it is PROVABLY correct
(the QC-speculative-decoding economics). The ``document`` and ``data`` families
have such a structural oracle; ``code`` and ``media`` historically did not, so
they always paid for a full smart-model review. This module supplies the missing
oracles, under one governing rule from Nemo's hull review:

    **An oracle proves the composite CONTAINS the declared units — not merely that
    it has their SHAPE.** Cheap PASS only where containment is *provably* attainable;
    otherwise honest fall-back to the full review ("an oracle where one *provably*
    exists"). A genuinely-broken assembly is never *failed* here — it simply gets the
    full review that rejects it.

Families:
  * **code** — wiring is statically checkable: a non-trivial entrypoint, every unit
    parses, and intra-package references resolve. EXTERNAL/SaaS/API-key'd imports are
    EXPECTED (an app *using* the user's tools), never a wiring failure.
  * **media/bundle** — the one provably-checkable media sub-kind: stdlib ``zipfile``
    is byte-preserving, so member-name-set + per-member byte equality prove containment.
  * **media/video,audio,image** — lossy composites with no cheap *post-hoc* containment
    proof (the only exact oracle is an assembler-emitted sidecar at composition time),
    so they honestly fall back to the full review this cut.

Every entrypoint is TOTAL: it catches every environmental/tool/parser failure and
converts it to ``(False, reason, "")`` so ``verify_assembly`` never throws (Nemo #6).

The **delegated-oracle** seam (Hero R1): an oracle resolved to a metered/API-key'd
tool must authorize through the Comptroller before it spends, and the verify mark
records *whose word* a PASS rested on (the ``oracle`` provenance id). Free-local
oracles (``zipfile``/``ast``) are ``cost_class`` free and never authorize.
"""

from __future__ import annotations

import ast
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from modulatio.review_ledger import ORACLE_BUNDLE, ORACLE_CODE, _norm_unit

if TYPE_CHECKING:
    from modulatio.assembly import AssemblyRecord
    from modulatio.comptroller import Authorization
    from modulatio.types import Task

#: the cost_class of a local/stdlib oracle — never spends, never authorizes.
FREE_LOCAL = "free-local"


@dataclass(frozen=True)
class Oracle:
    """A resolved validation oracle. ``run`` performs the actual check (already
    bound to its inputs) and returns ``(ok, reason)``. ``cost_class`` is
    ``free-local`` for the stdlib defaults; a registry-resolved metered tool
    carries ``paid-cloud``/``premium-cloud`` and authorizes before it runs."""

    name: str
    cost_class: str
    run: Callable[[], tuple[bool, str]]


def run_oracle(
    oracle: Oracle,
    *,
    authorize: "Callable[[], Authorization] | None" = None,
    fallback: Oracle | None = None,
) -> tuple[bool, str, str]:
    """Run ``oracle`` under the delegated-oracle contract (Hero R1a).

    * **free-local** (or ``None`` cost_class) → run directly, no authorization.
    * **metered** (paid-cloud/premium-cloud) → it is a verify-time SPEND, so it must
      authorize first via ``authorize`` (bound to ``comptroller.authorize_metered_tool``
      by the caller). On **deny / no-authorizer → fall back** to ``fallback`` (the
      stdlib default oracle) if provided, else to the full review — NEVER an unmetered
      external call.

    Returns ``(ok, reason, oracle_id)``. On a cheap PASS, ``oracle_id`` names the
    oracle that vouched (the provenance recorded in the verify mark, R1b); on any
    fall-back it is ``""`` (no cheap pass was granted on this oracle's word).
    """
    if oracle.cost_class and oracle.cost_class != FREE_LOCAL:
        if authorize is None:
            deny = (
                f"metered oracle {oracle.name!r} selected but no authorizer wired "
                f"— full review"
            )
            if fallback is not None:
                return _run_free_local(fallback)
            return False, deny, ""
        try:
            auth = authorize()
        except Exception as exc:  # noqa: BLE001 — total: an authorizer crash falls back
            deny = f"metered-oracle authorization crashed: {type(exc).__name__}({exc}) — full review"
            if fallback is not None:
                return _run_free_local(fallback)
            return False, deny, ""
        if not getattr(auth, "allowed", False):
            deny = (
                f"metered oracle {oracle.name!r} denied: "
                f"{getattr(auth, 'reason', 'no budget')} — full review"
            )
            if fallback is not None:
                return _run_free_local(fallback)
            return False, deny, ""
    return _run_free_local(oracle)


def _run_free_local(oracle: Oracle) -> tuple[bool, str, str]:
    """Run a (free-local or already-authorized) oracle, totally. A PASS carries the
    oracle's provenance id; a fail/crash carries ``""`` (no cheap pass)."""
    try:
        ok, reason = oracle.run()
    except Exception as exc:  # noqa: BLE001 — total entrypoint (Nemo #6)
        return False, f"validator crashed: {type(exc).__name__}({exc}) — full review", ""
    if ok:
        return True, "", oracle.name
    return False, reason, ""


# ── code-wiring oracle (Part 1) ───────────────────────────────────────────────


def _is_trivial_body(tree: ast.Module) -> bool:
    """True iff the module body has NO substantive statement — only docstring /
    ``pass`` / ``...`` / **imports** (Nemo code #3: an import-only ``main.py`` is
    dependency wiring, not "an app here"). A real body needs a def/class/assignment/
    call/``if __name__`` guard — *something that does work*. Runnability beyond that
    is semantic (#101)."""
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # docstring or a bare constant/`...` expression
        if isinstance(node, (ast.Pass, ast.Import, ast.ImportFrom)):
            continue  # pass / import-only — not a substantive body
        return False
    return True


def _local_namespaces(py_units: set[str]) -> set[str]:
    """The top-level dotted prefixes that are LOCAL packages/modules in the
    assembled set (so an absolute import of one is provably-local, not external):
    a root ``foo.py`` → ``foo``; a root ``foo/__init__.py`` → ``foo``."""
    ns: set[str] = set()
    for rel in py_units:
        parts = rel.split("/")
        if len(parts) == 1 and rel.endswith(".py"):
            ns.add(rel[:-3])
        elif parts[-1] == "__init__.py":
            ns.add(parts[0])
    return ns


def _has_module(py_units: set[str], dotted: str) -> bool:
    """Is ``dotted`` present in the assembled set as a module or a package?"""
    base = dotted.replace(".", "/")
    return f"{base}.py" in py_units or f"{base}/__init__.py" in py_units


def _is_local_package(parts: "list[str]", py_units: set[str]) -> bool:
    """Is the dotted path ``parts`` a local PACKAGE (has ``__init__.py``) in the set?"""
    init_rel = "/".join([*parts, "__init__.py"]) if parts else "__init__.py"
    return init_rel in py_units


def _pkg_init_names(parts: "list[str]", sources: "dict[str, str]") -> set[str]:
    """Top-level names BOUND in package ``parts``'s ``__init__.py`` (defs/classes/
    assignments/import aliases) — the attributes a ``from pkg import NAME`` may
    legitimately pull WITHOUT a submodule file. Re-exports (``from .x import y`` in
    the __init__) count. A package with no/empty/unparseable __init__ → empty set."""
    init_rel = "/".join([*parts, "__init__.py"]) if parts else "__init__.py"
    src = sources.get(init_rel)
    if not src:
        return set()
    try:
        tree = ast.parse(src, filename=init_rel)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _member_resolves(
    pkg_parts: "list[str]", name: str, py_units: set[str], sources: "dict[str, str]"
) -> bool:
    """Does ``name`` resolve under local package ``pkg_parts`` — as a submodule file
    OR a top-level name bound in the package ``__init__`` (Nemo code #1/#2: prove the
    member of a ``from pkg import name`` or fall back, never cheap-PASS it unproven)."""
    sub = "/".join([*pkg_parts, name]) if pkg_parts else name
    if f"{sub}.py" in py_units or f"{sub}/__init__.py" in py_units:
        return True  # a real submodule
    return name in _pkg_init_names(pkg_parts, sources)  # an __init__ attribute/re-export


def _dangling_ref(
    unit_rel: str, sources: "dict[str, str]", py_units: set[str]
) -> str | None:
    """Return a reason iff the Python unit at ``unit_rel`` has a PROVABLE intra-package
    dangling reference (Nemo #5 + code #1/#2 — the exact local-vs-external rule):

      * a **relative** import (``from .[mod] import names``) resolving inside the
        package root whose target module OR named member is absent, OR
      * an **absolute** import whose top-level prefix IS a local namespace but whose
        target module OR (for a local package) named member is absent.

    Everything else — stdlib, third-party, SaaS/API-key SDKs, any prefix not present
    locally, ``import *``, anything ambiguous — is EXTERNAL/unprovable and is NEVER a
    cheap-FAIL. Where a member CANNOT be proven absent it falls back (full review),
    not pass. Returns ``None`` when nothing is provably dangling.
    """
    source = sources[unit_rel]
    try:
        tree = ast.parse(source, filename=unit_rel)
    except SyntaxError as exc:
        return f"unit {unit_rel!r} does not parse: {exc}"
    pkg_parts = unit_rel.split("/")[:-1]  # the importing module's package path
    local_ns = _local_namespaces(py_units)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                dangle = _check_absolute_module(alias.name, local_ns, py_units, unit_rel)
                if dangle:
                    return dangle
            continue
        if not isinstance(node, ast.ImportFrom):
            continue

        # resolve the base package this `from ... import` targets
        if node.level:  # relative
            drop = node.level - 1
            if drop > len(pkg_parts):
                continue  # escapes the assembled root → ambiguous, never a fail
            base = pkg_parts[: len(pkg_parts) - drop]
        else:  # absolute
            if not node.module:
                continue
            if node.module.split(".")[0] not in local_ns:
                continue  # external/SaaS — EXPECTED, never a fail
            base = []

        if node.module:
            # `from [base.]module import names` — the MODULE must resolve...
            mod_parts = [*base, *node.module.split(".")]
            if not _has_module(py_units, ".".join(mod_parts)):
                return (
                    f"unit {unit_rel!r}: import 'from {'.' * node.level}{node.module}' "
                    f"resolves inside a local namespace but the module "
                    f"{'.'.join(mod_parts)!r} is absent from the assembled set"
                )
            chase = mod_parts  # ...and if it's a PACKAGE, chase the named members
            chase_pkg = _is_local_package(mod_parts, py_units)
        else:
            # `from . import names` — names live directly under `base` (a package)
            chase = base
            chase_pkg = True

        if chase_pkg:
            for alias in node.names:
                if alias.name == "*":
                    continue  # star import — can't enumerate; never a fail
                if not _member_resolves(chase, alias.name, py_units, sources):
                    dots = "." * node.level
                    mod = node.module or ""
                    return (
                        f"unit {unit_rel!r}: 'from {dots}{mod} import {alias.name}' "
                        f"targets a local package but the member {alias.name!r} is "
                        f"absent from the assembled set"
                    )
    return None


def _check_absolute_module(
    name: str, local_ns: set[str], py_units: set[str], unit_rel: str
) -> str | None:
    """``import a.b.c`` fails ONLY when its top-level prefix is a local namespace AND
    the target module is absent. External/ambiguous → never a fail."""
    top = name.split(".")[0]
    if top not in local_ns:
        return None  # external/third-party/SaaS — EXPECTED, never a wiring failure
    if not _has_module(py_units, name):
        return (
            f"unit {unit_rel!r}: absolute import {name!r} targets local namespace "
            f"{top!r} but its module is absent from the assembled set"
        )
    return None


def _read_units(
    assembly_task: "Task", record: "AssemblyRecord", artifacts_root: Path
) -> "tuple[dict[str, Path], list[str]]":
    """Resolve the manifest's unit names to safe on-disk paths. Returns
    ``(by_norm_name, py_rels)`` — a map normalized-name→path for every resolvable
    unit, and the list of normalized ``.py`` rel-paths. A unit that escapes the root
    is omitted (the shared set-equality check will then fail it)."""
    from modulatio.assembly import _safe_unit_path

    by_name: dict[str, Path] = {}
    py_rels: list[str] = []
    for raw in record.manifest.get("units", []):
        norm = _norm_unit(raw)
        path = _safe_unit_path(raw, artifacts_root)
        if path is None:
            continue
        by_name[norm] = path
        if norm.endswith(".py"):
            py_rels.append(norm)
    return by_name, py_rels


def _validate_code(
    record: "AssemblyRecord", assembly_task: "Task", artifacts_root: Path
) -> tuple[bool, str]:
    """The code-wiring oracle's check (free-local, ``ast``). PASS only when the
    wiring is provably sound; any inability to prove → fall back (full review)."""
    by_name, py_rels = _read_units(assembly_task, record, artifacts_root)
    units = [_norm_unit(u) for u in record.manifest.get("units", [])]
    if not units:
        return False, "code assembly: no units to validate"

    # Python-first + fail-OPEN (Nemo): a non-Python unit we cannot parse means we
    # cannot prove wiring → full review (never a false cheap-PASS).
    if any(not u.endswith(".py") for u in units):
        return False, "code assembly: non-Python unit(s) present — full review (no parser)"

    # Read every unit ONCE into a name→source map (the __init__ resolution + the
    # entrypoint check + the dangling-ref walk all read from it).
    sources: dict[str, str] = {}
    for norm in units:
        path = by_name.get(norm)
        if path is None or not path.is_file():
            return False, f"code assembly: unit {norm!r} missing on disk — full review"
        try:
            sources[norm] = path.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            return False, f"code assembly: unit {norm!r} unreadable: {exc}"

    # 1. entrypoint exists AND is non-trivial (not a path-present empty shell, Nemo
    #    #4; not an import-only shell, Nemo code #3).
    entry = record.manifest.get("entrypoint")
    if not (isinstance(entry, str) and entry.strip()):
        return False, "code assembly: no entrypoint declared — full review"
    entry_norm = _norm_unit(entry.strip())
    if entry_norm not in sources:
        return False, f"code assembly: entrypoint {entry_norm!r} not in the assembled set"
    entry_src = sources[entry_norm]
    if not entry_src.strip():
        return False, f"code assembly: entrypoint {entry_norm!r} is empty — full review"
    try:
        entry_tree = ast.parse(entry_src, filename=entry_norm)
    except SyntaxError as exc:
        return False, f"code assembly: entrypoint {entry_norm!r} does not parse: {exc}"
    if _is_trivial_body(entry_tree):
        return False, (
            f"code assembly: entrypoint {entry_norm!r} has no real body "
            f"(docstring/imports/pass only) — full review"
        )

    # 2. every unit parses + 3. intra-package references resolve.
    py_set = set(py_rels)
    for norm in units:
        try:
            ast.parse(sources[norm], filename=norm)
        except SyntaxError as exc:
            return False, f"code assembly: unit {norm!r} does not parse: {exc}"
        dangle = _dangling_ref(norm, sources, py_set)
        if dangle:
            return False, dangle
    return True, ""


def validate_code_assembly(
    record: "AssemblyRecord",
    assembly_task: "Task",
    artifacts_root: Path,
    *,
    oracle: Oracle | None = None,
    authorize: "Callable[[], Authorization] | None" = None,
) -> tuple[bool, str, str]:
    """Code-wiring validator entrypoint. Returns ``(ok, reason, oracle_id)``; total."""
    local = Oracle(
        name=ORACLE_CODE,
        cost_class=FREE_LOCAL,
        run=lambda: _validate_code(record, assembly_task, artifacts_root),
    )
    if oracle is None:
        return run_oracle(local)
    return run_oracle(oracle, authorize=authorize, fallback=local)


# ── media-composite oracle (Part 2) ───────────────────────────────────────────


def _media_sub_kind(record: "AssemblyRecord", by_name: "dict[str, Path]") -> str:
    from modulatio.assembly import _media_kind

    resolved = [(n, p) for n, p in by_name.items()]
    return _media_kind(record.manifest, resolved)


def _validate_bundle(
    record: "AssemblyRecord", assembly_task: "Task", artifacts_root: Path
) -> tuple[bool, str]:
    """The bundle oracle (free-local, stdlib ``zipfile``). Proves CONTAINMENT, not
    shape: member-name set == normalized unit set (no dupes), no traversal/absolute
    archive paths, and each member's BYTES equal its resolved unit file (Nemo #1,
    Hero m1). ``zipfile`` is byte-preserving so this is exact, not tolerance-based."""
    out = record.output_file
    if out is None or not Path(out).is_file():
        return False, "bundle: composited output missing on disk"
    by_name, _py = _read_units(assembly_task, record, artifacts_root)
    expected_names = set(by_name.keys())
    try:
        with zipfile.ZipFile(out) as zf:
            members = zf.namelist()
            member_norm = [_norm_unit(m) for m in members]
            # no duplicate member names
            if len(member_norm) != len(set(member_norm)):
                return False, "bundle: archive names a member more than once"
            # no path traversal / absolute archive paths
            for m in members:
                if m.startswith("/") or ".." in Path(m).parts:
                    return False, f"bundle: unsafe archive member path {m!r}"
            # member-name set == normalized unit set (containment, not count)
            if set(member_norm) != expected_names:
                missing = expected_names - set(member_norm)
                extra = set(member_norm) - expected_names
                return False, (
                    f"bundle: member set != unit set "
                    f"(missing={sorted(missing)}, extra={sorted(extra)})"
                )
            # each member's BYTES equal its unit file (m1: equality is the spec; the
            # zip CRC is only a fast pre-screen that gates which members to read).
            norm_to_member = {_norm_unit(m): m for m in members}
            for norm, unit_path in by_name.items():
                info = zf.getinfo(norm_to_member[norm])
                unit_bytes = unit_path.read_bytes()
                if info.CRC != (zipfile.crc32(unit_bytes) & 0xFFFFFFFF):
                    return False, f"bundle: member {norm!r} CRC != unit file"
                if zf.read(norm_to_member[norm]) != unit_bytes:
                    return False, f"bundle: member {norm!r} bytes != unit file"
    except (zipfile.BadZipFile, OSError, KeyError) as exc:
        return False, f"bundle: archive unreadable ({type(exc).__name__}: {exc})"
    return True, ""


def _validate_media(
    record: "AssemblyRecord", assembly_task: "Task", artifacts_root: Path
) -> tuple[bool, str]:
    by_name, _py = _read_units(record=record, assembly_task=assembly_task, artifacts_root=artifacts_root)
    kind = _media_sub_kind(record, by_name)
    if kind == "bundle":
        return _validate_bundle(record, assembly_task, artifacts_root)
    # video/audio/image: lossy composites with no cheap post-hoc containment proof —
    # honest fall-back to the full review this cut (Nemo #2/#3). The only exact cheap
    # oracle is an assembler-emitted provenance sidecar at composition time (deferred).
    return False, (
        f"media assembly ({kind}): no cheap containment oracle — full review "
        f"(deferred: assembler-emitted provenance sidecar)"
    )


def validate_media_assembly(
    record: "AssemblyRecord",
    assembly_task: "Task",
    artifacts_root: Path,
    *,
    oracle: Oracle | None = None,
    authorize: "Callable[[], Authorization] | None" = None,
) -> tuple[bool, str, str]:
    """Media-composite validator entrypoint. Returns ``(ok, reason, oracle_id)``; total.
    Only ``bundle`` can cheap-PASS; av/image always fall back this cut."""
    local = Oracle(
        name=ORACLE_BUNDLE,
        cost_class=FREE_LOCAL,
        run=lambda: _validate_media(record, assembly_task, artifacts_root),
    )
    if oracle is None:
        return run_oracle(local)
    return run_oracle(oracle, authorize=authorize, fallback=local)
