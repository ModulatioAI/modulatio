# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Built-in bug reporting — user-agnostic, no token, no account plumbing.

The way anyone reports a bug to an open-source project: open the project's
GitHub issue tracker with the report prefilled and file it in the browser. On a
headless / browserless host (e.g. a remote NoMachine box) the caller falls back
to the issue URL (copy it elsewhere) or to emailing :data:`CONTACT_EMAIL`.

Uses stdlib ``webbrowser`` + ``urllib`` — no new dependency, no GitHub token.
(The earlier direct GitHub-API submission needed a maintainer PAT with issues
scope on this repo — useful to no ordinary reporter — so it was removed.)
"""
from __future__ import annotations

import urllib.parse
import webbrowser

REPO = "ModulatioAI/modulatio"
_NEW_ISSUE = f"https://github.com/{REPO}/issues/new"

#: The browserless / no-GitHub-account fallback — email the report to the team.
#: The new-issue URL needs a browser and a GitHub account; on a headless or
#: remote box (or for a user with no GitHub login) a plain email is the one path
#: that always works. The report modals copy the report and point here.
CONTACT_EMAIL = "contact@modulatio.ai"


def prefilled_issue_url(title: str, body: str) -> str:
    """The browser new-issue URL with title + body prefilled. Truncates the body
    to keep the URL within sane length limits."""
    q = urllib.parse.urlencode({"title": title, "body": body[:6000]})
    return f"{_NEW_ISSUE}?{q}"


def open_issue(title: str, body: str) -> tuple[bool, str]:
    """Open the prefilled new-issue page in the user's browser. Returns
    ``(opened, url)`` — ``opened`` is False on a headless / browserless host
    (``webbrowser.open`` returns False or raises) so the caller can fall back to
    copying the URL or emailing the report. The URL is always returned."""
    url = prefilled_issue_url(title, body)
    try:
        opened = bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001 — any browser-launch failure → fall back
        opened = False
    return opened, url


def compose_body(description: str, steps: str, diagnostics: str = "") -> str:
    """Assemble the issue body from the operator's fields + optional
    diagnostics bundle."""
    parts = [description.strip()]
    if steps.strip():
        parts.append("## Steps to reproduce\n" + steps.strip())
    if diagnostics.strip():
        parts.append(diagnostics.strip())
    return "\n\n".join(p for p in parts if p)


__all__ = [
    "CONTACT_EMAIL",
    "REPO",
    "prefilled_issue_url",
    "open_issue",
    "compose_body",
]
