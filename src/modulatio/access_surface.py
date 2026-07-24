# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Production access inventory — the authority axes the conformance matrix
and the capability card measure completeness against.

This module is a LEAF (imports nothing from the package) so the production
constructors themselves consume it: ``SecurityRequest`` validates its
request class here, ``Tool`` validates its origin here, ``Capability``
validates its kind here. A new class/origin/kind therefore CANNOT be
emitted by production code until it joins the inventory — the constructor
raises — and a stale inventory value fails the exact-set guards, so drift
is impossible in either direction. Capability-snapshot sources are not
listed here: they derive from the snapshot assembler's own signature in
``permissions`` (the parameter→source map is checked against the live
signature at import).
"""
from __future__ import annotations

#: Capability kinds the broker asks about — each is also a request class.
#: Dynamic per-tool capabilities use the ``tool:<name>`` prefix family and
#: ride the ``capability`` request class.
CAPABILITY_KINDS = ("network", "file-write", "shell", "secret")

#: Resource/request classes the authorization chokepoint classifies. The
#: filesystem axis (path/exec), the capability kinds, MCP-origin calls,
#: the broker's capability-only ask surface, and the substrate posture.
REQUEST_CLASSES = (
    "path", "exec", *CAPABILITY_KINDS, "mcp", "capability", "substrate",
)

#: Operator surfaces mapped to their REAL approval bridge (module, attr).
#: The guard resolves every entry — a surface whose bridge does not import
#: or lacks the attr fails; the bridge-conformance suite witnesses each.
SURFACE_BRIDGES = {
    "tui": ("modulatio.tui.leader_prompt", "make_modal_prompt_fn"),
    "web": ("modulatio.web.actors", "ApprovalBroker"),
    "acp": ("modulatio.acp.server", "ACPServer"),
}
OPERATOR_SURFACES = tuple(SURFACE_BRIDGES)

#: Execution backends that run model-authored actions. Constants so the
#: emitters (snapshot facts, seat context) consume the SAME values the
#: matrix enumerates.
BACKEND_TOOL_LOOP = "modulatio-tool-loop"
BACKEND_CLAY_CONFINED = "clay-confined"
BACKEND_CLAY_INTERACTIVE = "clay-interactive"
EXECUTION_BACKENDS = (
    BACKEND_TOOL_LOOP, BACKEND_CLAY_CONFINED, BACKEND_CLAY_INTERACTIVE,
)

#: How a served tool entered the registry — stamped on every ``Tool`` at
#: construction and validated there, so a new origin cannot ship unmapped.
TOOL_ORIGINS = ("builtin", "service", "mcp-gated", "mcp-trusted")

#: Substrate states the sandbox reports — the EnforcementState values plus
#: the operator ``off`` profile (bypass), which never upgrades to full.
SUBSTRATE_STATES = (
    "sandboxed_full", "degraded_allowlist", "refused", "off",
)


def validate_request_class(value: str) -> str:
    """Raise unless ``value`` is a declared request class. Called by the
    production request constructor, so an undeclared class cannot be
    emitted — it fails at the emitter, not in a later audit."""
    if value not in REQUEST_CLASSES:
        raise ValueError(
            f"request class {value!r} is not in the declared access "
            f"inventory {REQUEST_CLASSES} — add it to the inventory (and "
            f"the matrix coverage) before emitting it")
    return value


def validate_tool_origin(value: str) -> str:
    """Raise unless ``value`` is a declared tool origin (stamped on every
    Tool at construction)."""
    if value not in TOOL_ORIGINS:
        raise ValueError(
            f"tool origin {value!r} is not in the declared access "
            f"inventory {TOOL_ORIGINS}")
    return value


def validate_capability_kind(value: str) -> str:
    """Raise unless ``value`` is a declared capability kind or a member of
    the dynamic ``tool:<name>`` family."""
    if value in CAPABILITY_KINDS or value.startswith("tool:"):
        return value
    raise ValueError(
        f"capability kind {value!r} is not in the declared access "
        f"inventory {CAPABILITY_KINDS} (or the 'tool:' family)")


def inventory_diff(
    declared: "tuple[str, ...] | frozenset[str]",
    produced: "set[str] | frozenset[str]",
) -> "tuple[tuple[str, ...], tuple[str, ...]]":
    """Exact-set comparison for the drift guards: returns
    ``(stale, unmapped)`` — declared-but-never-produced values and
    produced-but-undeclared values. Both must be empty; a subset check
    would let stale inventory extras pass forever."""
    declared_set = set(declared)
    produced_set = set(produced)
    return (
        tuple(sorted(declared_set - produced_set)),
        tuple(sorted(produced_set - declared_set)),
    )
