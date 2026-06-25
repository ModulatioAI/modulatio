# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Built-in bug reporting — file an issue against the Modulatio repo.

Direct submission to the GitHub issues API when the user has supplied a token
(``MODULATIO_GITHUB_TOKEN``); a graceful fallback to the prefilled new-issue URL
when they haven't (so the feature works with no account/token — they paste and
submit in the browser). Uses stdlib ``urllib`` — no new dependency.

The token is the user's own GitHub PAT (issues scope). It is read from the
environment at call time and never logged or stored by this module.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

REPO = "ModulatioAI/modulatio"
_API = f"https://api.github.com/repos/{REPO}/issues"
_NEW_ISSUE = f"https://github.com/{REPO}/issues/new"

#: The tokenless / browserless fallback — email the report to the team. The
#: GitHub API needs a token and the new-issue URL needs a browser; on a headless
#: or remote box neither is available, so a plain email is the one path that
#: always works. The send-log modal copies the report and points here.
CONTACT_EMAIL = "contact@modulatio.ai"


@dataclass(frozen=True)
class BugReportResult:
    submitted: bool          # True if the issue was created via the API
    url: str                 # the created issue URL, or the prefilled fallback URL
    detail: str = ""         # human-readable status / error reason


def github_token() -> str | None:
    tok = os.environ.get("MODULATIO_GITHUB_TOKEN")
    return tok.strip() if tok and tok.strip() else None


def prefilled_issue_url(title: str, body: str) -> str:
    """The browser new-issue URL with title + body prefilled (the no-token
    fallback). Truncates the body to keep the URL within sane length limits."""
    q = urllib.parse.urlencode({"title": title, "body": body[:6000]})
    return f"{_NEW_ISSUE}?{q}"


def submit_issue(
    title: str,
    body: str,
    *,
    token: str | None = None,
    labels: tuple[str, ...] = ("bug",),
    urlopen=None,
) -> BugReportResult:
    """File the issue. With a token → POST to the GitHub API and return the
    created issue URL. Without → return a prefilled new-issue URL to open in a
    browser. ``urlopen`` is injectable for tests."""
    token = token or github_token()
    if not token:
        return BugReportResult(
            submitted=False,
            url=prefilled_issue_url(title, body),
            detail="No MODULATIO_GITHUB_TOKEN — open this URL to file it yourself.",
        )
    payload = json.dumps(
        {"title": title, "body": body, "labels": list(labels)}
    ).encode("utf-8")
    req = urllib.request.Request(
        _API,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "modulatio",
        },
    )
    opener = urlopen or urllib.request.urlopen
    try:
        with opener(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return BugReportResult(
            submitted=True,
            url=data.get("html_url", ""),
            detail="Issue filed.",
        )
    except urllib.error.HTTPError as exc:
        reason = f"GitHub API error {exc.code}"
        if exc.code in (401, 403):
            reason += " (check the token's 'issues'/'repo' scope)"
        return BugReportResult(
            submitted=False, url=prefilled_issue_url(title, body), detail=reason,
        )
    except Exception as exc:  # network down, timeout, etc. — degrade to the URL
        return BugReportResult(
            submitted=False,
            url=prefilled_issue_url(title, body),
            detail=f"Couldn't reach GitHub ({type(exc).__name__}) — open the URL.",
        )


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
    "BugReportResult",
    "github_token",
    "prefilled_issue_url",
    "submit_issue",
    "compose_body",
]
