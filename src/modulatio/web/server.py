# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""``modulatio-api`` — the WebOS server entry point.

The console script always resolves (declared unconditionally in
pyproject.toml); when the ``[web]`` extra isn't installed it prints the
install hint and exits instead of ImportError-ing at launch.

Security posture (design §3.4): binds ``127.0.0.1`` by default. A
non-loopback ``--host`` REQUIRES the bearer token — generated once into
the config dir (0600) and printed so the operator can pair the browser.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import secrets
import sys

from modulatio import config
from modulatio.web.install import is_installed

#: The shipped default WebOS port. Overridable by the MODULATIO_WEB_PORT
#: settings knob, and by an explicit ``--port`` (which wins over both).
_DEFAULT_PORT = 8787

_INSTALL_HINT = (
    "modulatio-api needs the web extra. Install it with:\n"
    '    pip install "modulatio[web]"\n'
)


def _ensure_web_token() -> str:
    """Return the WebOS bearer token, generating it on first use.

    Lives beside the rest of the operator config (0600 — it guards every
    non-loopback API call). DOT-prefixed name: the converse Leader has
    standing file access to the config dir, and the tools' dotfile floor is
    what keeps credential files out of an agent's reach — the name IS the
    fence.
    """
    from modulatio import config

    token_file = config.CONFIG_DIR / ".web_token"
    legacy = config.CONFIG_DIR / "web_token"
    if not token_file.exists() and legacy.exists():
        legacy.rename(token_file)  # one-time migration from the pre-dot name
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    token_file.write_text(token + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    return token


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def run(argv: list[str] | None = None) -> None:
    if not is_installed():
        print(_INSTALL_HINT, file=sys.stderr, end="")
        raise SystemExit(1)

    # The same env-load contract every other entry point gets from cli.py:
    # install-root + vault .env BEFORE any engine work, so the Leader's
    # provider keys exist in the server's environment (without this,
    # converse 500s — the keys the TUI sees are invisible here). Then apply
    # the operator's saved settings-tab overrides so MODULATIO_WEB_PORT (and
    # any other knob) reaches this process.
    config.load_modulatio_env()
    config.apply_env_overrides()

    parser = argparse.ArgumentParser(
        prog="modulatio-api", description="Serve the Modulatio WebOS."
    )
    parser.add_argument("--host", default="127.0.0.1")
    # default=None so an explicit --port is distinguishable from the fallback
    # chain (--port > MODULATIO_WEB_PORT knob > shipped default).
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    port = args.port if args.port is not None else _resolve_port()

    token: str | None = None
    allowed_hosts: list[str] = []
    if not _is_loopback(args.host):
        token = _ensure_web_token()
        # The bound name itself joins the Host allowlist (the rebinding
        # fence otherwise serves loopback names only).
        allowed_hosts.append(args.host)
        print(
            f"Non-loopback bind ({args.host}) — API calls require the bearer "
            f"token from <config>/.web_token. Pair the browser with:\n"
            f"    {token}"
        )

    import uvicorn

    from modulatio.web.app import create_app

    uvicorn.run(
        create_app(bearer_token=token, allowed_hosts=allowed_hosts),
        host=args.host, port=port,
    )


def _resolve_port() -> int:
    """The default port: the MODULATIO_WEB_PORT knob if set to a valid port,
    else the shipped default. A malformed value falls back rather than
    crashing the launch."""
    raw = os.environ.get("MODULATIO_WEB_PORT", "").strip()
    if raw:
        try:
            port = int(raw)
            if 1 <= port <= 65535:
                return port
        except ValueError:
            pass
    return _DEFAULT_PORT
