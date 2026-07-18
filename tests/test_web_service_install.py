# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""The WebOS service installer: ``modulatio-api --install-service`` writes a
user-level systemd unit for the running entry point and enables it, so the
WebOS survives reboots without a manual relaunch. No sudo, no hardcoded
paths — the unit is generated from the resolved script location."""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio.web import install as web_install


@pytest.fixture()
def fake_env(monkeypatch, tmp_path):
    """A Linux-looking environment: resolvable script, systemctl present,
    captured subprocess calls, and an isolated unit directory."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr(web_install.subprocess, "run", fake_run)
    monkeypatch.setattr(web_install.sys, "platform", "linux")
    monkeypatch.setattr(web_install.shutil, "which", lambda name: {
        "systemctl": "/usr/bin/systemctl",
        "loginctl": "/usr/bin/loginctl",
        "modulatio-api": "/home/user/.local/bin/modulatio-api",
    }.get(name))
    monkeypatch.setattr(web_install, "_unit_dir", lambda: tmp_path / "systemd" / "user")
    return {"calls": calls, "unit_dir": tmp_path / "systemd" / "user"}


def _unit_path(env) -> Path:
    return env["unit_dir"] / "modulatio-api.service"


# === unit generation ===


def test_install_writes_unit_with_resolved_execstart(fake_env):
    ok, msg = web_install.install_service()
    assert ok, msg
    text = _unit_path(fake_env).read_text()
    assert "ExecStart=/home/user/.local/bin/modulatio-api" in text
    assert "Restart=always" in text
    # user units hang off default.target, not multi-user.target
    assert "WantedBy=default.target" in text


def test_install_carries_host_and_port_into_execstart(fake_env):
    ok, _ = web_install.install_service(host="0.0.0.0", port=9090)
    assert ok
    text = _unit_path(fake_env).read_text()
    assert "--host 0.0.0.0" in text
    assert "--port 9090" in text


def test_install_default_omits_host_port_flags(fake_env):
    ok, _ = web_install.install_service()
    assert ok
    text = _unit_path(fake_env).read_text()
    assert "--host" not in text
    assert "--port" not in text


# === activation sequence ===


def test_install_reloads_enables_and_attempts_linger(fake_env):
    ok, _ = web_install.install_service()
    assert ok
    cmds = fake_env["calls"]
    assert ["/usr/bin/systemctl", "--user", "daemon-reload"] in cmds
    assert ["/usr/bin/systemctl", "--user", "enable", "--now",
            "modulatio-api.service"] in cmds
    assert any(c[0] == "/usr/bin/loginctl" and "enable-linger" in c for c in cmds)


def test_linger_failure_is_a_warning_not_an_error(fake_env, monkeypatch):
    def failing_run(cmd, **kwargs):
        class _R:
            returncode = 1 if cmd[0].endswith("loginctl") else 0
            stdout = ""
            stderr = "linger denied"

        return _R()

    monkeypatch.setattr(web_install.subprocess, "run", failing_run)
    ok, msg = web_install.install_service()
    assert ok
    assert "linger" in msg.lower()


# === honest refusals ===


def test_install_refuses_off_linux(fake_env, monkeypatch):
    monkeypatch.setattr(web_install.sys, "platform", "darwin")
    ok, msg = web_install.install_service()
    assert not ok
    assert "linux" in msg.lower()
    assert not _unit_path(fake_env).exists()


def test_install_refuses_without_systemctl(fake_env, monkeypatch):
    monkeypatch.setattr(web_install.shutil, "which", lambda name: {
        "modulatio-api": "/home/user/.local/bin/modulatio-api",
    }.get(name))
    ok, msg = web_install.install_service()
    assert not ok
    assert "systemctl" in msg.lower()


def test_install_refuses_when_script_unresolvable(fake_env, monkeypatch):
    monkeypatch.setattr(web_install.shutil, "which", lambda name: {
        "systemctl": "/usr/bin/systemctl",
    }.get(name))
    monkeypatch.setattr(web_install.sys, "argv", ["python"])
    ok, msg = web_install.install_service()
    assert not ok
    assert "modulatio-api" in msg


# === uninstall ===


def test_uninstall_disables_and_removes_unit(fake_env):
    web_install.install_service()
    ok, _ = web_install.uninstall_service()
    assert ok
    assert not _unit_path(fake_env).exists()
    cmds = fake_env["calls"]
    assert ["/usr/bin/systemctl", "--user", "disable", "--now",
            "modulatio-api.service"] in cmds


def test_uninstall_without_unit_is_clean_noop(fake_env):
    ok, msg = web_install.uninstall_service()
    assert ok
    assert "no service" in msg.lower() or "not installed" in msg.lower()


# === CLI wiring ===


def test_api_flag_routes_to_installer_and_skips_server(monkeypatch, capsys):
    from modulatio.web import server as web_server

    monkeypatch.setattr(web_server, "is_installed", lambda: True)
    called = {}
    monkeypatch.setattr(
        web_install, "install_service",
        lambda host=None, port=None: called.update(h=host, p=port) or (True, "service on"),
    )
    with pytest.raises(SystemExit) as e:
        web_server.run(["--install-service", "--port", "9191"])
    assert e.value.code == 0
    assert called == {"h": None, "p": 9191}
    assert "service on" in capsys.readouterr().out
