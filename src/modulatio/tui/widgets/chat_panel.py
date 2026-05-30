# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Chat-panel widget — prompt input + kickoff button + response area.

Slice #20. The Prompt tab's content. Wraps the three primitives so the
tab can compose a single widget and the kickoff handler can locate them
by stable ids rather than scanning.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Input, Static


class ChatPanel(Vertical):
    """Prompt input + kickoff button + response area."""

    DEFAULT_CSS = """
    ChatPanel {
        padding: 1;
    }
    ChatPanel > Input {
        margin-bottom: 1;
    }
    ChatPanel > Button {
        margin-bottom: 1;
    }
    ChatPanel > #prompt-response {
        height: 1fr;
        padding: 1;
        background: $surface;
    }
    """

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Enter a goal or objective…", id="prompt-input")
        yield Button("Kick off", id="prompt-kickoff", variant="primary")
        yield Static(
            "(the orchestrator's summary will appear here after you kick off)",
            id="prompt-response",
        )


__all__ = ["ChatPanel"]
