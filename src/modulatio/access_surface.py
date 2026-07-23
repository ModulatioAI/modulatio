# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Production access descriptors — the single inventory of every authority
axis the conformance matrix and the capability card measure completeness
against. Each descriptor set names a production truth (the request classes
the gate classifies, the operator surfaces with a real approval bridge, the
execution backends, the tool origins, the substrate states); a drift-guard
test binds every set to the production constructor it describes, so a new
class/surface/backend/origin/state fails completeness until it is handled
rather than silently escaping both the matrix and the card.
"""
from __future__ import annotations

#: Resource/request classes the authorization chokepoint classifies. The
#: filesystem axis (path/exec) plus the capability kinds (network/file-write/
#: shell/secret), MCP-origin calls, and the substrate posture itself.
REQUEST_CLASSES = (
    "path", "exec", "network", "file-write", "shell", "secret", "mcp",
    "capability", "substrate",
)

#: Operator surfaces with a real approval bridge (a prompt can reach a human
#: and a decision returns). Each must be witnessed by the bridge-conformance
#: suite or carry an explicit inapplicability reason.
OPERATOR_SURFACES = ("tui", "web", "acp")

#: Execution backends that run model-authored actions. The engine tool loop,
#: the confined Clay seat (kickoff producer/QC, native tools + --safe-mode),
#: and the interactive Clay Leader (full loadout).
EXECUTION_BACKENDS = (
    "modulatio-tool-loop", "clay-confined", "clay-interactive",
)

#: Tool origins/families — how a served tool entered the registry, which
#: fixes what authority fences it. Enumerated as ORIGINS (not runtime tool
#: names) so a dynamic ``mcp__<server>__<tool>`` maps to an origin.
TOOL_ORIGINS = ("builtin", "service", "mcp-gated", "mcp-trusted")

#: Substrate states the sandbox reports — the three EnforcementState values
#: plus the operator ``off`` profile (bypass), which never upgrades to full.
SUBSTRATE_STATES = (
    "sandboxed_full", "degraded_allowlist", "refused", "off",
)

#: The authority sources the capability snapshot represents — kept equal to
#: ``permissions.CAPABILITY_AUTHORITY_SOURCES`` by a drift guard, so the
#: snapshot's completeness is checked against THIS production inventory, not
#: a constant the snapshot returns about itself.
CAPABILITY_SOURCES = (
    "mode", "substrate", "workspace", "standing_roots", "folders",
    "gate_grants", "broker_grants", "tool_loadout", "clay_confinement",
    "mcp_servers",
)
