# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Release-path integrity pins for the self-contained .deb build.

The bundle installs its runtime graph from a hash-pinned lock
(``--require-hashes``) and then installs the project wheel ``--no-deps``. That
combination verifies the *named* packages but, on its own, cannot notice a stale
lock: ``--no-deps`` skips the wheel's ``Requires-Dist`` check, so a newly added
project dependency missing from the lock would install green. ``build_deb.sh``
closes that with a ``pip check`` immediately after the wheel install. These
tests pin both the ordering of that release path and the fact that ``pip check``
really does fail on an unsatisfied dependency."""
from __future__ import annotations

import base64
import hashlib
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest

_BUILD_DEB = Path(__file__).resolve().parents[1] / "packaging" / "build_deb.sh"


def test_release_path_keeps_hashed_install_then_nodeps_then_pip_check():
    """The three release-install steps must stay present and in order:
    hashed-lock install → project wheel ``--no-deps`` → ``pip check``. If a
    future edit drops or reorders any of them, the drift gate is gone."""
    script = _BUILD_DEB.read_text()

    # Match the command tokens, not the prose in surrounding comments.
    i_lock = script.find('--require-hashes -r "$LOCK"')
    i_wheel = script.find('--no-deps "$WHEEL"')
    i_check = script.find("-m pip check")

    assert i_lock != -1, "hashed-lock install (--require-hashes -r) missing"
    assert i_wheel != -1, "project wheel install (--no-deps) missing"
    assert i_check != -1, "post-install consistency gate (pip check) missing"
    assert i_lock < i_wheel < i_check, (
        "release install steps out of order: expected hashed-lock install, "
        "then --no-deps wheel, then pip check"
    )


def _write_drift_probe_wheel(dest_dir: Path) -> Path:
    """A minimal, pure-python wheel that declares a dependency which cannot be
    satisfied — the artifact used to prove ``--no-deps`` installs
    green while ``pip check`` refuses."""
    name, version = "driftprobe", "0.0.0"
    distinfo = f"{name}-{version}.dist-info"
    absent = "modulatio-drift-absent-xyz"
    files = {
        f"{distinfo}/METADATA": (
            "Metadata-Version: 2.1\n"
            f"Name: {name}\n"
            f"Version: {version}\n"
            f"Requires-Dist: {absent}\n"
        ),
        f"{distinfo}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }

    def _record_line(path: str, data: str) -> str:
        digest = hashlib.sha256(data.encode()).digest()
        b64 = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return f"{path},sha256={b64},{len(data.encode())}"

    record = "\n".join(_record_line(p, d) for p, d in files.items())
    record += f"\n{distinfo}/RECORD,,\n"
    files[f"{distinfo}/RECORD"] = record

    wheel_path = dest_dir / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w") as zf:
        for path, data in files.items():
            zf.writestr(path, data)
    return wheel_path, absent


def test_pip_check_fails_on_nodeps_install_of_unsatisfied_wheel(tmp_path):
    """The mechanism the release path relies on: after a ``--no-deps`` install
    of a wheel whose ``Requires-Dist`` is absent, ``pip check`` exits non-zero
    and names the missing dependency. Proves the gate in ``build_deb.sh`` would
    actually catch lock/wheel drift, not just that the command is present."""
    env_dir = tmp_path / "venv"
    venv.create(env_dir, with_pip=True)
    py = env_dir / "bin" / "python"
    if not py.exists():  # non-POSIX layout — not the release target
        pytest.skip("no POSIX venv python on this platform")

    wheel_path, absent = _write_drift_probe_wheel(tmp_path)

    installed = subprocess.run(
        [str(py), "-m", "pip", "install", "--no-deps", "--no-index", str(wheel_path)],
        capture_output=True, text=True,
    )
    assert installed.returncode == 0, (
        f"--no-deps install should succeed (that's the gap): {installed.stderr}"
    )

    checked = subprocess.run(
        [str(py), "-m", "pip", "check"], capture_output=True, text=True,
    )
    assert checked.returncode != 0, "pip check must fail on the unsatisfied graph"
    assert absent in (checked.stdout + checked.stderr), (
        "pip check should name the missing dependency"
    )


if sys.platform == "win32":  # the .deb release target is Linux-only
    pytestmark = pytest.mark.skip(reason="release .deb path is Linux-only")
