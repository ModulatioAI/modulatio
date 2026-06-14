# SPDX-License-Identifier: Apache-2.0
"""R2 audit regression: daemon detach must REDIRECT fds 0/1/2, not free them.

The LOW/resource-leak finding (daemon.py:117) was that the detach block closed
the inherited terminal streams (freeing the raw fds 0/1/2) but never redirected
those fds onto the new devnull/log streams. The standard double-fork daemonize
pattern uses os.dup2 so fd 0 -> devnull and fds 1/2 -> the log file stay valid
and point where intended; otherwise any C-level / subprocess write to fd 1 or 2
lands on whatever fd the kernel next hands out.

We exercise the REAL daemon.start() detach path in a forked child (so we never
disturb the pytest runner's own fds) and have the child report, via a result
file, the post-detach state of fds 0/1/2.
"""

from __future__ import annotations

import os
import sys
import textwrap
import subprocess
from pathlib import Path


DRIVER = textwrap.dedent(
    """
    import os, sys, json

    config_dir = sys.argv[1]
    result_path = sys.argv[2]

    from pathlib import Path
    from modulatio import config, daemon
    config.CONFIG_DIR = Path(config_dir)

    def _probe(*a, **k):
        # Runs inside the detached child, after the fd redirection.
        info = {}
        for fd in (0, 1, 2):
            try:
                os.fstat(fd)
                info[str(fd)] = "open"
            except OSError as e:
                info[str(fd)] = "closed:%s" % e.errno
        # Where does fd 1 actually point? Resolve via /proc.
        try:
            info["fd1_target"] = os.readlink("/proc/self/fd/1")
        except OSError:
            info["fd1_target"] = "?"
        try:
            info["fd0_target"] = os.readlink("/proc/self/fd/0")
        except OSError:
            info["fd0_target"] = "?"
        # A raw write to fd 1 must succeed (lands in the log).
        try:
            os.write(1, b"probe-fd1\\n")
            info["write_fd1"] = "ok"
        except OSError as e:
            info["write_fd1"] = "err:%s" % e.errno
        with open(result_path, "w") as fh:
            json.dump(info, fh)
        os._exit(0)

    daemon._run_daemon = _probe
    pid = daemon.start(stub=True)
    # Parent: wait for the child to finish writing the result, then exit.
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
    """
)


def _run_detach_probe(tmp_path: Path) -> dict:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    result_path = tmp_path / "result.json"
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    repo_src = str(Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = repo_src + os.pathsep + env.get("PYTHONPATH", "")
    subprocess.run(
        [sys.executable, str(driver), str(config_dir), str(result_path)],
        env=env,
        timeout=30,
        check=True,
        capture_output=True,
    )
    import json

    assert result_path.exists(), "detached child never wrote its probe result"
    return json.loads(result_path.read_text())


def test_detach_keeps_fds_012_open_and_redirected(tmp_path):
    info = _run_detach_probe(tmp_path)
    # The bug: closing the old streams freed fds 0/1/2 and left them closed.
    assert info["0"] == "open", f"fd 0 was not redirected: {info}"
    assert info["1"] == "open", f"fd 1 was not redirected: {info}"
    assert info["2"] == "open", f"fd 2 was not redirected: {info}"
    # fd 1 must point at the daemon log; fd 0 at devnull.
    assert "daemon.log" in info["fd1_target"], info
    assert "null" in info["fd0_target"], info
    # A raw fd-1 write (e.g. from a C extension) must not fail.
    assert info["write_fd1"] == "ok", info
