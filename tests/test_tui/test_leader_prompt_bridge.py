# SPDX-License-Identifier: Apache-2.0
"""The converse-worker → UI-thread approval bridge (``make_modal_prompt_fn``).

The Leader's permission gate calls ``prompt_fn(request)`` from inside the tool
loop, which runs on the converse WORKER THREAD. The bridge must:
  * show ``LeaderApprovalModal`` on the UI thread (via ``call_from_thread``),
  * BLOCK only the worker thread (never the event loop — that would deadlock
    the whole TUI) until the operator decides,
  * return a ``ScopedDecision`` carrying the chosen scope,
  * default to DENY (fail-closed) if dismissed with nothing.

These are real cross-thread integration tests: a background thread calls the
bridge while the test drives the modal on the UI side.
"""
from __future__ import annotations

import threading

from textual.app import App, ComposeResult
from textual.widgets import Static

from modulatio import leader_permissions as lp
from modulatio import leader_gate as lg
from modulatio.tui.leader_prompt import make_modal_prompt_fn
from modulatio.tui.widgets.leader_approval_modal import LeaderApprovalModal


class _Host(App[None]):
    def get_css_variables(self) -> dict[str, str]:
        v = super().get_css_variables()
        v.setdefault("frame", "#6cb6e4")
        v.setdefault("frame-dim", "#3f6d8c")
        return v

    def compose(self) -> ComposeResult:
        yield Static("host")


def _req(**kw):
    base = dict(action="edit", resource="/home/cknox/proj/x.py",
                request_class="path", why="edit_file x.py")
    base.update(kw)
    return lg.SecurityRequest(**base)


async def _run_bridge_and_click(app, pilot, request, button_or_key, *, is_key=False):
    """Spawn a worker calling the bridge, wait for the modal, drive it, join."""
    prompt_fn = make_modal_prompt_fn(app)
    box: dict = {}

    def worker():
        box["decision"] = prompt_fn(request)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    # Wait for the modal to mount on the UI thread.
    for _ in range(100):
        await pilot.pause()
        if isinstance(app.screen, LeaderApprovalModal):
            break
    assert isinstance(app.screen, LeaderApprovalModal), "modal never appeared"
    if is_key:
        await pilot.press(button_or_key)
    else:
        await pilot.click(button_or_key)
    # Let the dismiss callback fire + the worker unblock.
    for _ in range(100):
        await pilot.pause()
        if not t.is_alive():
            break
    t.join(timeout=2)
    assert not t.is_alive(), "worker thread never unblocked — deadlock"
    return box["decision"]


async def test_bridge_returns_chosen_scope():
    app = _Host()
    async with app.run_test() as pilot:
        decision = await _run_bridge_and_click(app, pilot, _req(), "#scope-session")
        assert isinstance(decision, lg.ScopedDecision)
        assert decision.scope == lp.SCOPE_SESSION


async def test_bridge_always_scope_round_trips():
    app = _Host()
    async with app.run_test() as pilot:
        decision = await _run_bridge_and_click(app, pilot, _req(), "#scope-always")
        assert decision.scope == lp.SCOPE_ALWAYS


async def test_bridge_escape_is_deny_fail_closed():
    app = _Host()
    async with app.run_test() as pilot:
        decision = await _run_bridge_and_click(app, pilot, _req(), "escape", is_key=True)
        assert decision.scope == lp.SCOPE_DENY


async def test_bridge_computes_broad_root_warning():
    """The warning shown is engine-computed (dangerous_widen_root), not model
    text — a broad root surfaces a warning in the modal."""
    app = _Host()
    async with app.run_test() as pilot:
        prompt_fn = make_modal_prompt_fn(app)
        box: dict = {}
        t = threading.Thread(target=lambda: box.__setitem__("d", prompt_fn(_req(resource="/home"))),
                             daemon=True)
        t.start()
        for _ in range(100):
            await pilot.pause()
            if isinstance(app.screen, LeaderApprovalModal):
                break
        text = " ".join(str(s.render()) for s in app.screen.query(Static))
        assert "broad system/home" in text
        await pilot.press("escape")
        for _ in range(100):
            await pilot.pause()
            if not t.is_alive():
                break
        assert box["d"].scope == lp.SCOPE_DENY
