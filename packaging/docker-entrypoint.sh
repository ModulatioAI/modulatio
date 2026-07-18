#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
# Container entrypoint — one image, four run modes:
#   api    serve the WebOS (default; binds loopback by default. To expose it
#          on the network, pass an explicit host — `api --host 0.0.0.0` — and
#          front it with TLS; the server's bearer-token + Host allowlist still
#          govern access, but the transport is plaintext.)
#   tui    the terminal UI (use: docker exec -it <name> modulatio-tui,
#          or run interactively with -it)
#   ssh    the TUI-over-SSH door (sshd in the foreground; sessions drop
#          straight into modulatio-tui via ForceCommand)
#   *      anything else execs verbatim (modulatio …, bash, …)
set -eu

# Clay auto-detect (zero-config). The image ships no Claude; if a host or
# sidecar Claude is mounted at /opt/claude (the shipped compose does this by
# default), point MODULATIO_CLAUDE_BIN at the NEWEST version present so
# find_claude_binary resolves it. Nothing mounted → left unset → Clay is simply
# unavailable and everything else runs. Never pins a version.
if [ -z "${MODULATIO_CLAUDE_BIN:-}" ]; then
  _claude="$(ls -d /opt/claude/versions/* 2>/dev/null | sort -V | tail -1 || true)"
  [ -n "$_claude" ] && [ -x "$_claude" ] && export MODULATIO_CLAUDE_BIN="$_claude"
fi

case "${1:-api}" in
  api)  shift || true; exec modulatio-api "$@" ;;
  tui)  shift || true; exec modulatio-tui "$@" ;;
  ssh)
    # sshd wants root to bind + fork session users; the image runs as
    # `modulatio`, so the ssh service in compose sets `user: root`.
    exec /usr/sbin/sshd -D -e ;;
  *)    exec "$@" ;;
esac
