# SPDX-License-Identifier: Apache-2.0
"""Tests for built-in bug reporting — user-agnostic: open the project's GitHub
issue tracker (prefilled) in a browser, or fall back to the issue URL / email.
No token, no API submission (that was maintainer-only plumbing)."""
from __future__ import annotations

from modulatio import bug_report


def test_compose_body_assembles_sections():
    body = bug_report.compose_body("it broke", "1. do x\n2. boom", "## Diagnostics\nv1")
    assert "it broke" in body
    assert "Steps to reproduce" in body
    assert "1. do x" in body
    assert "## Diagnostics" in body


def test_prefilled_issue_url_targets_the_public_repo():
    url = bug_report.prefilled_issue_url("My bug", "details here")
    assert url.startswith("https://github.com/ModulatioAI/modulatio/issues/new")
    assert "My" in url and "bug" in url  # title prefilled (url-encoded)


def test_open_issue_opens_the_browser_and_returns_url(monkeypatch):
    opened_with: dict = {}
    monkeypatch.setattr(
        bug_report.webbrowser, "open",
        lambda u: opened_with.setdefault("url", u) is None or True,
    )
    ok, url = bug_report.open_issue("boom", "body")
    assert ok is True
    assert url == opened_with["url"]
    assert url.startswith("https://github.com/ModulatioAI/modulatio/issues/new")


def test_open_issue_reports_failure_on_headless_host(monkeypatch):
    """No browser (NoMachine / headless): webbrowser.open returns False or
    raises — open_issue must report ok=False so the caller falls back to copy."""
    def _boom(_url):
        raise RuntimeError("no browser")

    monkeypatch.setattr(bug_report.webbrowser, "open", _boom)
    ok, url = bug_report.open_issue("boom", "body")
    assert ok is False
    assert url.startswith("https://github.com/ModulatioAI/modulatio/issues/new")


def test_open_issue_false_when_no_browser_controller(monkeypatch):
    monkeypatch.setattr(bug_report.webbrowser, "open", lambda _u: False)
    ok, _url = bug_report.open_issue("boom", "body")
    assert ok is False


def test_token_machinery_is_gone():
    """The maintainer-only GitHub-API submit + token reader are removed — a
    user-agnostic product never asks a bug reporter for a token."""
    assert not hasattr(bug_report, "submit_issue")
    assert not hasattr(bug_report, "github_token")
    assert not hasattr(bug_report, "BugReportResult")
