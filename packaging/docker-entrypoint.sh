#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
# Container entrypoint — one image, four run modes:
#   api    serve the WebOS (default; --host 0.0.0.0 so it is reachable
#          beyond the container — the server's own bearer-token + Host
#          allowlist govern non-loopback access)
#   tui    the terminal UI (use: docker exec -it <name> modulatio-tui,
#          or run interactively with -it)
#   ssh    the TUI-over-SSH door (sshd in the foreground; sessions drop
#          straight into modulatio-tui via ForceCommand)
#   *      anything else execs verbatim (modulatio …, bash, …)
set -eu

case "${1:-api}" in
  api)  shift || true; exec modulatio-api --host 0.0.0.0 "$@" ;;
  tui)  shift || true; exec modulatio-tui "$@" ;;
  ssh)
    # sshd wants root to bind + fork session users; the image runs as
    # `modulatio`, so the ssh service in compose sets `user: root`.
    exec /usr/sbin/sshd -D -e ;;
  *)    exec "$@" ;;
esac
