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
    """Enumerate request classes from PRODUCTION emitters — the gate's
    filesystem classes, the capability-kind dispatch table's RANGE (never
    the descriptor echoed back), plus the three engine-emitted classes
    with their emit sites named: ``mcp`` (leader_gate MCP extraction),
    ``capability`` (the coordinator's capability-only ask), ``substrate``
    (the snapshot's posture fact)."""
    from modulatio import leader_gate as lg

    produced = set(lg._FS_CLASSES)
    produced.update(perm.PRODUCTION_CAPABILITY_KINDS)
    produced.update({"mcp", "capability", "substrate"})
    return produced


def _production_capability_kinds(tmp_path) -> set:
    """Fixed kinds enumerated by calling ``capability_for`` over the REAL
    built registry plus the dispatch table's own range — a declared kind
    with no dispatch rule cannot appear here."""
    registry = tools_mod.build_registry(
        artifacts_root=tmp_path, tool_calls_dir=tmp_path / "tc")
    produced = {
        perm.capability_for(name).kind
        for name in registry
        if not perm.capability_for(name).kind.startswith("tool:")
    }
    produced.update(perm.CAPABILITY_KIND_BY_TOOL.values())
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


def test_every_mapped_tool_emits_exactly_its_mapped_kind():
    """The table is AUTHORITATIVE for dispatch: every mapped tool's real
    capability carries exactly the mapped kind — a mapping that does not
    route cannot sit in the table unnoticed."""
    for tool_name, kind in perm.CAPABILITY_KIND_BY_TOOL.items():
        assert perm.capability_for(tool_name, {}).kind == kind


def test_dispatch_validation_passes_on_the_real_table():
    perm.validate_capability_dispatch()


def test_mapped_entry_that_stops_emitting_a_fixed_kind_fails_fast(
    monkeypatch,
):
    """The table cannot be updated alongside the inventory while the
    emitter still returns the dynamic ``tool:<name>`` capability —
    production validation raises."""
    monkeypatch.setitem(
        perm.CAPABILITY_BUILDERS, "phantom_probe_tool",
        lambda name, args: perm.Capability(
            f"tool:{name}", "phantom", "",
            _scoped={"once": f"tool:{name}", "session": f"tool:{name}",
                     "always": f"tool:{name}"}))
    with pytest.raises(ValueError):
        perm.validate_capability_dispatch()


def test_unmapped_tool_still_gets_the_dynamic_capability():
    """Tools outside the table legitimately fall through — the dynamic
    family is the default, not a dispatch failure."""
    cap = perm.capability_for("some_unmapped_tool", {})
    assert cap.kind == "tool:some_unmapped_tool"


def test_capability_kinds_exact_against_dispatch_range(tmp_path):
    """The declared kinds equal what production DISPATCH emits — a kind no
    rule produces (the old stale ``secret``) fails as stale."""
    stale, unmapped = axs.inventory_diff(
        axs.CAPABILITY_KINDS, _production_capability_kinds(tmp_path))
    assert stale == () and unmapped == ()
    assert set(axs.CAPABILITY_KINDS) == set(perm.PRODUCTION_CAPABILITY_KINDS)


def test_descriptor_only_capability_kind_fails_as_stale(tmp_path):
    stale, unmapped = axs.inventory_diff(
        axs.CAPABILITY_KINDS + ("secret",),
        _production_capability_kinds(tmp_path))
    assert stale == ("secret",)


def test_registered_tool_origins_are_exact_inventory(tmp_path, monkeypatch):
    """Origins enumerated from CONSTRUCTED production tools — the real
    builtin registry, the real service builder over a fixture catalog, and
    the real MCP tool builder over both trust postures — equal the
    inventory exactly. No expected strings are inserted by the test."""
    from modulatio import mcp_client, mcp_config, service_tools, services

    registry = tools_mod.build_registry(
        artifacts_root=tmp_path, tool_calls_dir=tmp_path / "tc")
    produced = {t.origin for t in registry.values()}

    fixture_service = services.Service(
        id="svc-fixture", name="svc", kind="custom",
        capabilities=("image", "research"), env_var="SVC_KEY",
        base_url="https://svc.example", auth_shape="bearer",
        free_tier=True)
    monkeypatch.setattr(
        services, "load_services", lambda: {"svc-fixture": fixture_service})
    monkeypatch.setattr(
        service_tools.services, "load_services",
        lambda: {"svc-fixture": fixture_service})
    service_built = service_tools.build_service_tools(tmp_path)
    assert service_built, "the real service builder must construct tools"
    produced.update(t.origin for t in service_built.values())

    for trust in mcp_config._TRUST:
        server = mcp_config.McpServer(
            id="fx", name="fixture", transport="stdio", trust=trust)
        built = mcp_client.build_server_tool(
            name=f"mcp__fx__probe_{trust}", description="d",
            schema={"type": "object"}, cost=None, server=server,
            call=lambda **kw: "ok")
        produced.add(built.origin)

    stale, unmapped = axs.inventory_diff(axs.TOOL_ORIGINS, produced)
    assert stale == () and unmapped == ()


def test_execution_backends_enumerate_from_real_builders():
    """Backends come from markers the REAL production builders stamp on
    themselves — the engine loop and the constructed Clay runner — never
    from constants echoed back."""
    from modulatio import runners

    produced = {runners.run_llm_with_tools.execution_backend}
    clay = runners._build_claude_cli_chat_runner(
        "anthropic/claude-probe", "claude-probe")
    produced.update(clay.execution_backends)
    stale, unmapped = axs.inventory_diff(axs.EXECUTION_BACKENDS, produced)
    assert stale == () and unmapped == ()


def test_operator_surfaces_enumerate_from_bridge_markers():
    """The produced surface set is the ``approval_surface`` markers the
    real bridge objects stamp on themselves at their definition sites."""
    produced = set()
    for surface, (module_path, attr) in axs.SURFACE_BRIDGES.items():
        bridge = getattr(importlib.import_module(module_path), attr)
        marker = bridge.approval_surface
        assert marker == surface, (surface, marker)
        produced.add(marker)
    stale, unmapped = axs.inventory_diff(axs.OPERATOR_SURFACES, produced)
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
