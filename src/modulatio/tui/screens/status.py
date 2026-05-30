# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Status tab content (slice #21 + auth-alerts banner + Step 0 toggles).

One team-aggregate ``ActivityLog`` plus one per default-role panel
(leader / planner / drafter / qc / researcher). ModulatioApp's
activity-callback routes each event to the team log AND the matching
per-role log in one hop.

Adds an ``AuthAlertBanner`` at the top that polls
``~/.config/modulatio/auth_alerts.json`` mtime once per second and
re-renders when active alerts change. Self-clears the moment a successful
dispatch removes the alert.

Step 0 round-2 M2 (Lovecraft audit, 2026-05-16): adds a
``TogglesBanner`` that surfaces opt-in env vars (currently
``MODULATIO_LEADER_ITERATE``) so users can see at a glance which
optional paths are active without grep'ing the source. STATUS_ROLES
swapped "coordinator" → "planner" to match the new role labels the
orchestrator emits in activity events (M3).
"""
from __future__ import annotations

import os

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Label, Static

from modulatio import auth_alerts
from modulatio.tui.widgets.activity_log import ActivityLog


STATUS_ROLES: tuple[str, ...] = ("leader", "planner", "drafter", "qc", "researcher")


class AuthAlertBanner(Static):
    """A red banner listing active auth alerts. Polls the alerts file
    mtime so external clears (e.g. successful dispatch from the daemon
    in another process) get picked up live."""

    DEFAULT_CSS = """
    AuthAlertBanner {
        background: $error 30%;
        color: $text;
        padding: 1 2;
        margin-bottom: 1;
        text-style: bold;
        height: auto;
    }
    AuthAlertBanner.no-alerts {
        display: none;
    }
    """

    def __init__(self) -> None:
        super().__init__("")
        self._last_mtime: float | None = None
        self._refresh_render()

    def on_mount(self) -> None:
        # 1s poll is well below human-perception latency for "did the
        # alert clear?" while staying cheap (single small JSON read).
        self.set_interval(1.0, self._poll)

    def _poll(self) -> None:
        from modulatio import config
        try:
            mtime = config.AUTH_ALERTS_FILE.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._last_mtime:
            self._last_mtime = mtime
            self._refresh_render()

    def _refresh_render(self) -> None:
        alerts = auth_alerts.load_alerts()
        if not alerts:
            self.add_class("no-alerts")
            self.update("")
            return
        self.remove_class("no-alerts")
        lines = ["⚠ AUTH ALERT" + ("S" if len(alerts) > 1 else "")]
        for pid, alert in alerts.items():
            lines.append(f"  {pid}: {alert.get('error_message', '')[:140]}")
            lines.append(f"     Fix: {alert.get('suggested_fix', '')}")
        self.update("\n".join(lines))


class TogglesBanner(Static):
    """Static banner listing opt-in env-var toggles.

    Currently surfaces ``MODULATIO_LEADER_ITERATE``. Read once at
    compose time — env doesn't change mid-session, so polling is
    overhead the banner doesn't need.
    """

    DEFAULT_CSS = """
    TogglesBanner {
        background: $boost;
        color: $text-muted;
        padding: 0 2;
        margin-bottom: 1;
        height: auto;
    }
    """

    def __init__(self) -> None:
        super().__init__("")
        self._refresh_render()

    def _refresh_render(self) -> None:
        iterate_on = os.environ.get("MODULATIO_LEADER_ITERATE") == "1"
        iterate_badge = "ON " if iterate_on else "OFF"
        self.update(
            f"Toggles  [{iterate_badge}] MODULATIO_LEADER_ITERATE "
            "(Leader between-task reflection)"
        )


class StatusPanel(VerticalScroll):
    """Status tab — auth banner + toggles banner + team feed + per-role activity panels."""

    DEFAULT_CSS = """
    StatusPanel {
        padding: 1;
    }
    StatusPanel Label {
        margin-top: 1;
        text-style: bold;
    }
    """

    def compose(self) -> ComposeResult:
        yield AuthAlertBanner()
        yield TogglesBanner()
        yield Label("Team activity")
        yield ActivityLog(id="status-team-log")
        yield Label("Per-role")
        for role in STATUS_ROLES:
            yield Label(role.capitalize())
            yield ActivityLog(id=f"status-role-{role}", filter_role=role)


def build_status_panel() -> StatusPanel:
    return StatusPanel()


__all__ = ["STATUS_ROLES", "StatusPanel", "AuthAlertBanner", "TogglesBanner", "build_status_panel"]
