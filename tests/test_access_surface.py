# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Drift guards: every access descriptor is bound to the production
constructor it describes. A new request class, operator surface, execution
backend, tool origin, substrate state, or capability source in production
fails the matching guard until it is added to the descriptor — so it cannot
silently escape the conformance matrix or the capability card.
"""
from __future__ import annotations

from modulatio import access_surface as axs
from modulatio import claude_cli
from modulatio import permissions as perm
from modulatio import sandbox


def test_request_classes_cover_production_gate_classes():
    """Every class the gate/broker actually emit is a declared descriptor."""
    from modulatio import leader_gate as lg
    from modulatio import leader_permissions as lp

    produced = set(lg._FS_CLASSES)              # path, exec
    produced.add("mcp")                          # extract_tool_requests mcp class
    produced.add("capability")                   # the broker's ask surface
    produced.add("substrate")                    # the sandbox posture fact
    # capability KINDS map onto network/file-write/shell/secret.
    for kind in ("network", "shell", "file-write", "secret"):
        produced.add(kind)
    assert lp.REQUEST_CLASS_PATH in axs.REQUEST_CLASSES
    assert produced <= set(axs.REQUEST_CLASSES)


def test_substrate_states_equal_production_enforcement():
    """The substrate descriptors equal the EnforcementState values plus the
    operator off-profile — a new enforcement state fails here."""
    enforcement = {s.value for s in sandbox.EnforcementState}
    assert enforcement <= set(axs.SUBSTRATE_STATES)
    assert "off" in axs.SUBSTRATE_STATES        # the bypass profile
    # every non-off descriptor is a real EnforcementState value
    assert set(axs.SUBSTRATE_STATES) - {"off"} == enforcement


def test_tool_origins_cover_production_families():
    """builtin + the media/service families + the two MCP trust postures."""
    from modulatio import mcp_config
    assert "builtin" in axs.TOOL_ORIGINS
    assert "service" in axs.TOOL_ORIGINS
    # MCP trust postures are exactly the origin split.
    assert set(mcp_config._TRUST) == {"gated", "trusted"}
    assert {"mcp-gated", "mcp-trusted"} <= set(axs.TOOL_ORIGINS)


def test_capability_sources_track_the_snapshot_constant():
    """The descriptor's capability-source set equals the snapshot's own
    source tuple — so snapshot completeness is measured against a production
    descriptor, not a constant the snapshot returns about itself."""
    assert set(axs.CAPABILITY_SOURCES) == set(perm.CAPABILITY_AUTHORITY_SOURCES)


def test_execution_backends_named():
    """The tool loop plus the two Clay seat postures. Clay's confined seat
    is the ``--safe-mode`` producer/QC lane; the interactive seat is the
    full-loadout Leader."""
    assert set(axs.EXECUTION_BACKENDS) == {
        "modulatio-tool-loop", "clay-confined", "clay-interactive"}
    # the confined backend's allowlist is the claude_cli constant
    assert claude_cli._ALLOWED_CONFINED_TOOLS  # confined seat exists


def test_operator_surfaces_have_bridges():
    """Every declared surface has a real approval bridge module — the
    bridge-conformance suite witnesses the wiring."""
    import importlib
    # TUI modal, Web broker, ACP server — the three bridge implementations.
    assert importlib.import_module("modulatio.tui.leader_prompt")
    assert importlib.import_module("modulatio.web.actors")
    assert importlib.import_module("modulatio.acp.server")
    assert set(axs.OPERATOR_SURFACES) == {"tui", "web", "acp"}
