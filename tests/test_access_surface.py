# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Drift guards: the access inventory and production constructors bind in
BOTH directions. Production consumes the inventory at construction (an
undeclared class/origin/kind cannot be emitted — the constructor raises),
and every guard here compares the inventory to production-enumerated
values with EXACT set equality — a stale inventory extra fails just as a
new unmapped production value does.
"""
from __future__ import annotations

import importlib

import pytest

from modulatio import access_surface as axs
from modulatio import claude_cli
from modulatio import permissions as perm
from modulatio import sandbox
from modulatio import tools as tools_mod


def _production_request_classes() -> set:
    """Enumerate request classes from the PRODUCTION constants that emit
    them — the gate's filesystem classes, the broker's capability kinds,
    plus the three engine-emitted classes (mcp extraction, the broker's
    capability-only ask, the substrate posture fact)."""
    from modulatio import leader_gate as lg

    produced = set(lg._FS_CLASSES)
    produced.update(axs.CAPABILITY_KINDS)
    produced.update({"mcp", "capability", "substrate"})
    return produced


def test_request_classes_exact_set_against_production():
    stale, unmapped = axs.inventory_diff(
        axs.REQUEST_CLASSES, _production_request_classes())
    assert stale == () and unmapped == ()


def test_stale_inventory_value_fails_exact_validation():
    """A descriptor-only value that no production constructor emits is
    caught — subset checks would let it pass forever."""
    stale, unmapped = axs.inventory_diff(
        axs.REQUEST_CLASSES + ("teleport",), _production_request_classes())
    assert stale == ("teleport",)


def test_new_production_class_fails_exact_validation():
    """A production constructor emitting a new class fails completeness
    until the inventory maps it."""
    produced = _production_request_classes() | {"quantum"}
    stale, unmapped = axs.inventory_diff(axs.REQUEST_CLASSES, produced)
    assert unmapped == ("quantum",)


def test_undeclared_request_class_cannot_be_constructed():
    """The emitter itself refuses an undeclared class — drift cannot even
    reach the store, let alone the matrix."""
    from modulatio import leader_gate as lg

    with pytest.raises(ValueError):
        lg.SecurityRequest(
            action="a", resource="r", request_class="quantum", why="w")


def test_undeclared_tool_origin_cannot_be_constructed():
    with pytest.raises(ValueError):
        tools_mod.Tool(
            name="x", description="d", call=lambda: "ok", origin="alien")


def test_undeclared_capability_kind_cannot_be_constructed():
    with pytest.raises(ValueError):
        perm.Capability(kind="psychic", label="x")
    # the dynamic per-tool family stays constructible
    assert perm.Capability(kind="tool:web_search", label="x")


def test_registered_tool_origins_are_exact_inventory(tmp_path):
    """Origins enumerated from PRODUCTION-registered tools (builtin
    registry + the two MCP trust postures + the service family) equal the
    inventory exactly."""
    from modulatio import mcp_config

    registry = tools_mod.build_registry(
        artifacts_root=tmp_path, tool_calls_dir=tmp_path / "tc")
    produced = {t.origin for t in registry.values()}
    produced.update(f"mcp-{trust}" for trust in mcp_config._TRUST)
    produced.add("service")            # service_tools stamps this family
    stale, unmapped = axs.inventory_diff(axs.TOOL_ORIGINS, produced)
    assert stale == () and unmapped == ()


def test_substrate_states_equal_production_enforcement():
    enforcement = {s.value for s in sandbox.EnforcementState}
    stale, unmapped = axs.inventory_diff(
        axs.SUBSTRATE_STATES, enforcement | {"off"})
    assert stale == () and unmapped == ()


def test_capability_sources_derive_from_assembler_signature():
    """The source inventory is DERIVED from the assembler's parameter→
    source map, and import-time validation pins that map to the live
    signature — no second tuple exists to go stale. Every declared source
    is fed by at least one real parameter."""
    import inspect

    params = set(inspect.signature(
        perm.effective_capability_snapshot).parameters)
    assert params == set(perm._SOURCE_BY_PARAM)
    assert set(perm.CAPABILITY_AUTHORITY_SOURCES) == set(
        perm._SOURCE_BY_PARAM.values())


def test_unmapped_fact_source_cannot_be_assembled():
    with pytest.raises(ValueError):
        perm.CapabilityFact(
            source="astrology", request_class="capability",
            resource="x", state=perm.STATE_AVAILABLE)


def test_invented_state_vocabulary_cannot_be_assembled():
    with pytest.raises(ValueError):
        perm.CapabilityFact(
            source="mode", request_class="capability",
            resource="x", state="Sort of allowed")


def test_execution_backends_are_named_constants():
    """Emitters consume the SAME constants the matrix enumerates."""
    assert set(axs.EXECUTION_BACKENDS) == {
        axs.BACKEND_TOOL_LOOP, axs.BACKEND_CLAY_CONFINED,
        axs.BACKEND_CLAY_INTERACTIVE}
    assert claude_cli._ALLOWED_CONFINED_TOOLS  # the confined seat exists


def test_operator_surfaces_resolve_their_real_bridges():
    """Every surface maps to a resolvable (module, attr) bridge; the set
    of surfaces IS the bridge map's keys — no parallel list."""
    assert axs.OPERATOR_SURFACES == tuple(axs.SURFACE_BRIDGES)
    for surface, (module_path, attr) in axs.SURFACE_BRIDGES.items():
        module = importlib.import_module(module_path)
        assert hasattr(module, attr), (surface, module_path, attr)
