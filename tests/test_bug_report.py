# SPDX-License-Identifier: Apache-2.0
"""Tests for built-in bug reporting (GitHub submission + fallback)."""
from __future__ import annotations

import json
import urllib.error


from modulatio import bug_report


class _FakeResp:
    def __init__(self, data: dict) -> None:
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return json.dumps(self._data).encode("utf-8")


def test_compose_body_assembles_sections():
    body = bug_report.compose_body("it broke", "1. do x\n2. boom", "## Diagnostics\nv1")
    assert "it broke" in body
    assert "Steps to reproduce" in body
    assert "1. do x" in body
    assert "## Diagnostics" in body


def test_no_token_returns_prefilled_url(monkeypatch):
    monkeypatch.delenv("MODULATIO_GITHUB_TOKEN", raising=False)
    r = bug_report.submit_issue("My bug", "details here", token=None)
    assert r.submitted is False
    assert r.url.startswith(
        "https://github.com/ModulatioAI/modulatio/issues/new")
    assert "My" in r.url and "bug" in r.url  # title prefilled (url-encoded)


def test_with_token_files_the_issue():
    def fake_urlopen(req, timeout=None):
        # the request carries the auth header + JSON payload
        assert req.headers["Authorization"].startswith("Bearer ")
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["title"] == "boom"
        return _FakeResp({"html_url":
                          "https://github.com/ModulatioAI/modulatio/issues/42"})

    r = bug_report.submit_issue("boom", "body", token="tok", urlopen=fake_urlopen)
    assert r.submitted is True
    assert r.url.endswith("/issues/42")


def test_http_error_degrades_to_prefilled(monkeypatch):
    monkeypatch.delenv("MODULATIO_GITHUB_TOKEN", raising=False)

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    r = bug_report.submit_issue("t", "b", token="badtok", urlopen=fake_urlopen)
    assert r.submitted is False
    assert "401" in r.detail
    assert r.url.startswith(
        "https://github.com/ModulatioAI/modulatio/issues/new")


def test_github_token_reads_env(monkeypatch):
    monkeypatch.setenv("MODULATIO_GITHUB_TOKEN", "  ghp_xyz  ")
    assert bug_report.github_token() == "ghp_xyz"
    monkeypatch.delenv("MODULATIO_GITHUB_TOKEN", raising=False)
    assert bug_report.github_token() is None
