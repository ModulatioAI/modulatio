# SPDX-License-Identifier: Apache-2.0
"""LOW-audit regressions for ``modulatio.sandbox``.

Uniquely-named to avoid colliding with ``test_sandbox.py`` while the
0.9.0 stability wave edits run concurrently.
"""

from __future__ import annotations

from modulatio import sandbox


def test_parent_pwd_not_forwarded_into_sandbox(tmp_path, monkeypatch):
    """#84: the parent's ``PWD`` must NOT cross into the child env.

    The child is launched with its own ``cwd`` (the artifacts dir), so a
    forwarded parent ``PWD`` would be a stale value that disagrees with the
    real working directory and leaks the parent's path. The env dict the
    sandbox hands to ``subprocess.run`` must simply omit ``PWD``.
    """
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    monkeypatch.setenv("PWD", "/home/operator/some/stale/parent/dir")
    monkeypatch.setenv("HOME", "/home/test")

    _, env = sandbox.build_sandboxed_argv(["echo"], artifacts)

    assert "PWD" not in env, (
        "parent PWD leaked into the sandbox env — child cwd is the "
        "artifacts dir, so a forwarded PWD is stale and wrong"
    )
    # The rest of the safe allowlist still flows through.
    assert env["HOME"] == "/home/test"


def test_pwd_absent_from_safe_env_keys():
    """#84: PWD is no longer on the static forward allowlist."""
    assert "PWD" not in sandbox._SAFE_ENV_KEYS
