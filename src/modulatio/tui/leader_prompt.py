# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""The converse-worker → UI-thread bridge for the Leader permission gate.

``LeaderPermissionGate.decide`` calls ``prompt_fn(request) -> ScopedDecision``
synchronously from inside the tool loop — which runs on the converse WORKER
THREAD (``@work(thread=True)`` in app.py). Textual modals live on the UI thread.
``make_modal_prompt_fn`` bridges the two safely:

  * It schedules the modal push via ``app.call_from_thread`` — and the scheduled
    callable only PUSHES the screen (non-blocking, callback-based), so the event
    loop is never blocked waiting on the operator. (Blocking the loop would
    deadlock the whole TUI — the modal could never render.)
  * The worker thread blocks on a ``threading.Event`` until the operator picks a
    scope; the TUI stays fully responsive meanwhile.
  * The default is DENY (fail-closed): if the modal is dismissed with nothing,
    or the scope is empty, the request is refused.

The modal's warning is ENGINE-computed here (``dangerous_widen_root``), never
model-narrated.
"""
from __future__ import annotations

import threading

from modulatio import leader_gate as lg
from modulatio import leader_permissions as lp
from modulatio.tui.widgets.leader_approval_modal import LeaderApprovalModal


def make_modal_prompt_fn(app):
    """Return a ``prompt_fn(request) -> ScopedDecision`` bound to ``app``, safe
    to call from the converse worker thread."""

    def prompt_fn(request) -> lg.ScopedDecision:
        # Engine-computed risk warning for path widens (never model text).
        warning = None
        if request.request_class == lp.REQUEST_CLASS_PATH:
            warning = lg.dangerous_widen_root(request.resource)

        box = {"scope": lp.SCOPE_DENY}  # fail-closed default
        done = threading.Event()

        def show():
            def on_dismiss(scope):
                box["scope"] = scope or lp.SCOPE_DENY
                done.set()

            app.push_screen(LeaderApprovalModal(request, warning=warning), on_dismiss)

        # Schedule the push on the UI thread; returns once `show` (just the
        # push) completes — it does NOT wait on the operator.
        app.call_from_thread(show)
        # Block ONLY this worker thread until the operator decides.
        done.wait()
        return lg.ScopedDecision(scope=box["scope"])

    return prompt_fn


__all__ = ["make_modal_prompt_fn"]

# This callable IS the TUI approval bridge — it stamps its surface
# identity from the declared inventory so the completeness guard
# enumerates surfaces from the real bridge objects.
from modulatio import access_surface as _axs  # noqa: E402 — leaf module

make_modal_prompt_fn.approval_surface = _axs.SURFACE_TUI
