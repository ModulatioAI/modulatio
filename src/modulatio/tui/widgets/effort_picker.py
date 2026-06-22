# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""EffortPicker — optional add-model step for Codex (GPT-5.5) seats: pick the
reasoning effort (xhigh / high / medium / low). Medium is recommended; the
ChatGPT backend's default very-high effort causes heavy reasoning-token bloat
(slow, over-long runs). Selecting one posts ``EffortChosen``.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from modulatio import provider_catalog as pc


class EffortPicker(Vertical):
    """Pick a Codex reasoning effort → ``EffortChosen``."""

    DEFAULT_CSS = """
    EffortPicker { height: 1fr; padding: 1; border: round $frame; }
    EffortPicker .ep-title { text-style: bold; color: $primary; }
    EffortPicker #ep-list { height: 1fr; border: round $frame-dim; }
    """

    class EffortChosen(Message):
        def __init__(self, effort: str) -> None:
            self.effort = effort
            super().__init__()

    def compose(self) -> ComposeResult:
        yield Static(
            "Reasoning effort for GPT-5.5 — medium recommended (xhigh bloats tokens)",
            classes="ep-title",
        )
        yield OptionList(id="ep-list")

    def on_mount(self) -> None:
        ol = self.query_one("#ep-list", OptionList)
        for e in pc.CODEX_REASONING_EFFORTS:
            label = f"{e}  (recommended)" if e == pc.CODEX_DEFAULT_EFFORT else e
            ol.add_option(Option(label, id=e))
        try:
            ol.highlighted = list(pc.CODEX_REASONING_EFFORTS).index(pc.CODEX_DEFAULT_EFFORT)
        except ValueError:
            pass
        ol.focus()

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        self.post_message(self.EffortChosen(event.option.id or pc.CODEX_DEFAULT_EFFORT))
