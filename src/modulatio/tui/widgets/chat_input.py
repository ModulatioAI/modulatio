# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""ChatInput — the Console composer. Enter SENDS; it never launches a job.

A ``TextArea`` subclass where:
  - **Enter** posts a ``Submitted`` message (send to the Leader) instead of
    inserting a newline — so hitting Enter can never fire a job run;
  - **Shift+Enter** / **Ctrl+J** insert a newline (compose a multi-line
    message), where the terminal distinguishes them.

Launching a job is a deliberate, bracketed action — type
``/kickoff <objective> /end`` in this composer; a plain Enter only ever sends a
message to the Leader, never a job run.
"""
from __future__ import annotations

from textual import events
from textual.message import Message
from textual.widgets import TextArea


class ChatInput(TextArea):
    """Composer text box: Enter sends, Shift+Enter newlines."""

    class Submitted(Message):
        """Posted when the operator presses Enter with non-empty text."""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            text = self.text.strip()
            if text:
                self.post_message(self.Submitted(text))
            return
        if event.key in ("shift+enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        await super()._on_key(event)


__all__ = ["ChatInput"]
