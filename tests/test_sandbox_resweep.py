"""0.9.0 pre-ship re-sweep regressions for the bubblewrap sandbox layer.

Dedicated file (does not touch test_sandbox.py) for two re-sweep findings:

* #163 — ``_DENY_ENV_PATTERNS`` missed secret-shaped ``pass_env`` names whose
  ``KEY`` suffix lacked the exact ``_KEY`` shape (e.g. ``MISTRAL_APIKEY``,
  ``DEPLOY_KEYS``). They must now be denied.
* #418 — ``build_sandboxed_argv``'s ``extra_binds`` docstring claimed it gives
  pytest the test tree / venv, but the production caller never wires it. The
  docstring must no longer make that false claim, and the hook must still work
  for callers that DO pass it (back-compat).
"""

from __future__ import annotations

import pytest

from modulatio import sandbox


# ── #163: broadened secret-key deny patterns ─────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "MISTRAL_APIKEY",   # no underscore before KEY
        "FOO_APIKEY",
        "API-KEY",          # hyphenated
        "DEPLOY_KEYS",      # plural
        "SIGNING_KEYS",
        "MY_KEY",           # still covered (regression guard for old _KEY$)
        "API_KEY",
        "SOMEKEY",          # bare suffix, no separator
    ],
)
def test_secret_shaped_names_are_denied(name):
    assert sandbox._is_safe_env_name(name) is False


@pytest.mark.parametrize(
    "name",
    [
        "HOME",
        "PROJECT_NAME",
        "KEYBOARD_LAYOUT",  # 'KEY' as a prefix, not a key suffix → safe
        "MONKEYPATCH",      # contains 'KEY' mid-word, not a secret shape
    ],
)
def test_non_secret_names_still_pass(name):
    assert sandbox._is_safe_env_name(name) is True


def test_apikey_dropped_from_pass_env(monkeypatch, tmp_path):
    """End-to-end: a skill that lists MISTRAL_APIKEY in pass_env must not
    have it forwarded into the sandboxed child env."""
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    monkeypatch.setenv("MISTRAL_APIKEY", "secret-mistral")
    monkeypatch.setenv("DEPLOY_KEYS", "secret-deploy")
    _, env = sandbox.build_sandboxed_argv(
        ["echo", "hi"], artifacts, pass_env=("MISTRAL_APIKEY", "DEPLOY_KEYS"),
    )
    assert "MISTRAL_APIKEY" not in env
    assert "DEPLOY_KEYS" not in env


# ── #418: stale extra_binds docstring ────────────────────────────────────


def test_extra_binds_docstring_not_false_pytest_claim():
    doc = sandbox.build_sandboxed_argv.__doc__ or ""
    # The stale claim was that extra_binds is "used to give pytest access to
    # the project's test tree and venv site-packages". It must be gone.
    assert "give pytest access" not in doc
    # And the docstring should now mark it as an unused hook.
    assert "UNUSED" in doc or "unused" in doc


def test_extra_binds_hook_still_functional(tmp_path):
    """Back-compat: callers that DO pass extra_binds still get a ro-bind."""
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    argv, _ = sandbox.build_sandboxed_argv(
        ["echo"], artifacts, extra_binds=(extra,),
    )
    assert "--ro-bind-try" in argv
    assert str(extra.resolve()) in argv
