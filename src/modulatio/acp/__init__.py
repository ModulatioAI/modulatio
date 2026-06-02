# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Agent Client Protocol (ACP) server for Modulatio.

Lets an external client (an editor like Zed) drive Modulatio's conversational
Leader over JSON-RPC-on-stdio: prompt turns, live activity, and
client-approved tool calls. See :func:`modulatio.acp.server.run_acp_server`.
"""
from modulatio.acp.server import run_acp_server

__all__ = ["run_acp_server"]
