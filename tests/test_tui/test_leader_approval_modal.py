# SPDX-License-Identifier: Apache-2.0
"""LeaderApprovalModal — the engine-rendered permission prompt.

The modal renders ONLY engine-parsed ``SecurityRequest`` fields (action /
canonical resource / why) — never model-narrated text (Wild Bill MED-5) — and
offers exactly the scope buttons in ``request.available_scopes`` (a destructive
request that omits ``always`` shows no Always button). Each button dismisses the
modal with its scope string; Escape / Deny dismisses with ``deny`` (fail-closed).
"""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Button, Static

from modulatio import leader_gate as lg
from modulatio import leader_permissions as lp
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


async def test_modal_renders_engine_fields_not_freeform():
    app = _Host()
    async with app.run_test() as pilot:
        modal = LeaderApprovalModal(_req())
        await app.push_screen(modal)
        await pilot.pause()
        text = " ".join(str(s.render()) for s in modal.query(Static))
        assert "/home/cknox/proj/x.py" in text   # canonical resource shown
        assert "edit" in text                     # the action
        assert "edit_file x.py" in text           # engine-rendered why


async def test_all_four_scopes_offered_by_default():
    app = _Host()
    async with app.run_test() as pilot:
        modal = LeaderApprovalModal(_req())
        await app.push_screen(modal)
        await pilot.pause()
        ids = {b.id for b in modal.query(Button)}
        assert ids == {"scope-once", "scope-session", "scope-always", "scope-deny"}


async def test_available_scopes_filters_buttons():
    app = _Host()
    async with app.run_test() as pilot:
        # destructive: only once / deny may be offered
        req = _req(action="delete",
                   available_scopes=(lp.SCOPE_ONCE, lp.SCOPE_DENY))
        modal = LeaderApprovalModal(req)
        await app.push_screen(modal)
        await pilot.pause()
        ids = {b.id for b in modal.query(Button)}
        assert ids == {"scope-once", "scope-deny"}
        assert "scope-always" not in ids


async def test_clicking_always_dismisses_with_always_scope():
    app = _Host()
    captured = {}
    async with app.run_test() as pilot:
        modal = LeaderApprovalModal(_req())
        app.push_screen(modal, lambda scope: captured.__setitem__("scope", scope))
        await pilot.pause()
        await pilot.click("#scope-always")
        await pilot.pause()
        assert captured["scope"] == lp.SCOPE_ALWAYS


async def test_escape_dismisses_with_deny_fail_closed():
    app = _Host()
    captured = {}
    async with app.run_test() as pilot:
        modal = LeaderApprovalModal(_req())
        app.push_screen(modal, lambda scope: captured.__setitem__("scope", scope))
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert captured["scope"] == lp.SCOPE_DENY


async def test_broad_root_warning_shown_when_present():
    app = _Host()
    async with app.run_test() as pilot:
        modal = LeaderApprovalModal(_req(resource="/home"),
                                    warning="/home is a broad system/home directory")
        await app.push_screen(modal)
        await pilot.pause()
        text = " ".join(str(s.render()) for s in modal.query(Static))
        assert "broad system/home" in text
