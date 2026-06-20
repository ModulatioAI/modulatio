# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Live Claude CLI round-trip test — skipped in CI (no creds) and on any host
without bwrap. When it runs it spends a tiny fraction of the Claude subscription
and confirms the full sandbox harness end-to-end."""

import os
import pytest


@pytest.mark.skipif(
    not os.path.exists(os.path.expanduser("~/.claude/.credentials.json")),
    reason="no Claude Code login (~/.claude) — live Clay test skipped",
)
def test_live_clay_roundtrip(tmp_path):
    """LIVE: a real `claude -p` round-trip through the subscription harness,
    confined to a temp workspace. Skipped without Claude Code creds (CI)."""
    from modulatio import claude_cli, oauth_helpers, sandbox
    if not sandbox.is_sandbox_available():
        pytest.skip("bwrap not available; Clay is sandbox-required")
    claude_bin = oauth_helpers.find_claude_binary()
    assert claude_bin, "claude not found"
    out = claude_cli.run_claude(
        claude_bin=claude_bin, model="claude-haiku-4-5",
        prompt="Reply with exactly: CLAY_OK", workspace=tmp_path, add_dirs=[],
        timeout=120.0,
    )
    assert "CLAY_OK" in out
