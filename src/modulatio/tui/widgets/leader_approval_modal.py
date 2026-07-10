# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""LeaderApprovalModal — the cross-cutting Leader permission prompt.

This is the single approval surface for every gated Leader request (folder
widening first; ``exec`` / ``network`` / ``spend`` later). It renders ONLY
engine-parsed ``SecurityRequest`` fields — action, canonical resource, why —
NEVER model-narrated prose, so the Leader cannot prompt-inject
the operator into granting "always". It offers exactly the scopes in
``request.available_scopes``; each button dismisses with its scope string, and
Escape dismisses with ``deny`` (fail-closed).

Pushed with a callback (works from the converse worker thread via the app's
``call_from_thread`` bridge — see ``_leader_prompt_fn`` in app.py):

    self.push_screen(LeaderApprovalModal(request, warning=...), on_dismiss)
"""
from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from modulatio import leader_permissions as lp

#: Engine-fixed labels per scope — order is the button order.
_SCOPE_BUTTONS = (
    (lp.SCOPE_ONCE, "Once", "primary"),
    (lp.SCOPE_SESSION, "This session", "primary"),
    (lp.SCOPE_ALWAYS, "Always", "warning"),
    (lp.SCOPE_DENY, "Deny", "error"),
)


class LeaderApprovalModal(ModalScreen[str]):
    """Asks the operator to scope a gated Leader request. Dismisses with a scope
    string (``once`` / ``session`` / ``always`` / ``deny``)."""

    BINDINGS = [Binding("escape", "deny", "Deny")]

    DEFAULT_CSS = """
    LeaderApprovalModal {
        align: center middle;
    }
    LeaderApprovalModal #leader-approval-modal {
        width: 72;
        max-width: 90%;
        height: auto;
        padding: 1 2;
        border: round $frame;
        background: $surface;
    }
    LeaderApprovalModal #la-title { text-style: bold; padding-bottom: 1; }
    LeaderApprovalModal #la-why { color: $text-muted; padding-bottom: 1; }
    LeaderApprovalModal #la-warning { color: $warning; padding-bottom: 1; }
    LeaderApprovalModal #la-buttons { height: 3; }
    LeaderApprovalModal #la-buttons Button { margin-right: 1; }
    """

    def __init__(self, request, *, warning: str | None = None) -> None:
        super().__init__()
        self._request = request
        self._warning = warning

    def compose(self) -> ComposeResult:
        req = self._request
        with Vertical(id="leader-approval-modal"):
            # Engine-rendered: action + canonical resource. NEVER model text.
            yield Static(
                escape(f"The Leader wants to {req.action}: {req.resource}"),
                id="la-title",
            )
            yield Static(escape(f"Reason: {req.why}"), id="la-why")
            if self._warning:
                yield Static(escape(f"⚠ {self._warning}"), id="la-warning")
            with Horizontal(id="la-buttons"):
                for scope, label, variant in _SCOPE_BUTTONS:
                    if scope in req.available_scopes:
                        yield Button(label, id=f"scope-{scope}", variant=variant)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # button id is "scope-<scope>" — split once, dismiss with the scope.
        scope = (event.button.id or "").split("scope-", 1)[-1]
        self.dismiss(scope or lp.SCOPE_DENY)

    def action_deny(self) -> None:
        """Escape = fail-closed deny."""
        self.dismiss(lp.SCOPE_DENY)


__all__ = ["LeaderApprovalModal"]
